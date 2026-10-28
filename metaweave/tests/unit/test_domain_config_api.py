"""doc 24.1、6.2 §5.2：domain 配置编辑 API（工作区定位新模型）。

按工作区定位（read/write_domain_config(settings, workspace_id)）；准入 =
domain 目录顶层唯一成功采集 marker；保存前 ``backup_files_before_edit``
文件级备份（.backup/bak_时间戳/，只复制将被覆盖文件，不触碰 marker）；
备份失败本次保存终止且原文件/marker 不动（A28）；指纹对原始字节计算。
"""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml

from backend.profile_loading.domain_config_api import (
    DomainConfigBackupFailedError,
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
from backend.workspace_files.backups import EditBackupFailedError
from backend.workspace_files.markers import (
    list_collection_markers,
    write_collection_marker,
)
from backend.data_sources.domain_step_service import verify_domain_artifact
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
DB_NAME = "orders"
SETTINGS = SimpleNamespace(database_url="fake")


class FakeSession:
    """工作区行锁（session.get with_for_update）与源行分发的最小假会话。"""

    def __init__(self, workspace_row=None, source_row=None):
        self.workspace_row = workspace_row
        self.source_row = source_row
        self.events: list[str] = []

    def get(self, model, key, with_for_update=None):
        from backend.persistence.models import DbSourceInfo, WorkspaceInfo

        if model is WorkspaceInfo:
            if with_for_update:
                self.events.append("lock")
            if self.workspace_row is None or key != self.workspace_row.workspace_id:
                return None
            return self.workspace_row
        if model is DbSourceInfo:
            if self.source_row is None or key != self.source_row.source_id:
                return None
            return self.source_row
        raise AssertionError(f"unexpected model {model}")


@contextmanager
def _fake_platform_session(session):
    yield session


def _workspace_row():
    return SimpleNamespace(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID)


def _source_row(db_name=DB_NAME, schemas=("public",), is_active=True):
    return SimpleNamespace(
        source_id=SOURCE_ID,
        db_name=db_name,
        is_active=is_active,
        selected_schemas=list(schemas),
        description="数据源描述",
    )


def _session(**kwargs):
    return FakeSession(_workspace_row(), _source_row(**kwargs))


def _install(
    monkeypatch,
    tmp_path: Path,
    session: FakeSession | None = None,
    *,
    marker: str | None = "success",
):
    """打桩会话与目录解析；marker=None 时不写采集 marker。"""
    import backend.profile_loading.domain_config_api as api

    session = session if session is not None else _session()
    root = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    domain_dir = root / "domain"
    md_dir = root / "md"
    domain_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    if marker is not None:
        write_collection_marker(domain_dir, RUN_ID, marker)

    monkeypatch.setattr(
        api, "platform_session", lambda settings: _fake_platform_session(session)
    )
    monkeypatch.setattr(api, "get_db_source", lambda _session, _sid: session.source_row)
    monkeypatch.setattr(
        "backend.workspaces.repository.get_workspace",
        lambda _session, _wid: session.workspace_row,
    )
    monkeypatch.setattr(api, "resolve_domain_output_dir", lambda s, w: domain_dir)
    monkeypatch.setattr(api, "resolve_md_output_dir", lambda s, w: md_dir)
    return root


def _write_yaml(root: Path, text: str) -> None:
    (root / "domain" / "db_domains.yaml").write_text(text, encoding="utf-8")


def _write_manifest(root: Path, files: list[str]) -> None:
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


def _payload(view: dict, domains: list) -> dict:
    return {
        "fingerprint": view["fingerprint"],
        "database": view["database"],
        "max_domains_per_table": view["max_domains_per_table"],
        "domains": domains,
    }


# ---- GET：指纹与配置视图 ----


def test_get_sample_and_stable_fingerprint(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    first = read_domain_config(SETTINGS, WORKSPACE_ID)
    second = read_domain_config(SETTINGS, WORKSPACE_ID)
    assert first["fingerprint"] == second["fingerprint"]
    assert len(first["fingerprint"]) == 64
    assert first["domains"][0]["tables"] == ["orders.public.users"]
    assert first["database"]["description"] == "库描述原文"
    assert first["max_domains_per_table"] == 3
    # 指纹对原始字节计算
    raw = (root / "domain" / "db_domains.yaml").read_bytes()
    assert first["fingerprint"] == hashlib.sha256(raw).hexdigest()


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
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, text)
    with pytest.raises(DomainConfigInvalidError) as exc:
        read_domain_config(SETTINGS, WORKSPACE_ID)
    assert "domains" not in exc.value.details
    assert reason_part in exc.value.details["reason"]


def test_get_missing_tables_key_is_empty_list(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        "database:\n  name: orders\n  description: x\ndomains:\n  - name: 甲\n    description: 一\n",
    )
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    assert view["domains"][0]["tables"] == []


def test_get_does_not_reject_table_outside_candidates(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        "database:\n  name: orders\n  description: x\ndomains:\n"
        "  - name: 甲\n    description: 一\n    tables: ['orders.public.gone']\n",
    )
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    assert view["domains"][0]["tables"] == ["orders.public.gone"]


def test_max_domains_missing_or_invalid_returns_null_and_write_keeps_original(
    tmp_path, monkeypatch
):
    root = _install(monkeypatch, tmp_path)
    text = (
        "database:\n  name: orders\n  description: 库\n"
        "llm_inference:\n  max_domains_per_table: 0\n"
        "domains:\n  - name: 甲\n    description: 一\n    tables: []\n"
    )
    _write_yaml(root, text)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    assert view["max_domains_per_table"] is None
    saved = write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, view["domains"]))
    assert saved["max_domains_per_table"] is None
    written = yaml.safe_load(
        (root / "domain" / "db_domains.yaml").read_text(encoding="utf-8")
    )
    assert written["llm_inference"]["max_domains_per_table"] == 0


# ---- 候选表清单（md manifest 交叉校验） ----


def test_candidates_filter_and_manifest_errors(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
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
    listed = list_domain_tables(SETTINGS, WORKSPACE_ID)
    assert listed["tables"] == [{"id": "orders.public.users", "label": "public.users"}]

    (root / "md" / "manifest.json").unlink()
    with pytest.raises(DomainConfigInvalidError) as exc:
        list_domain_tables(SETTINGS, WORKSPACE_ID)
    assert "重跑 md" in exc.value.details["reason"]
    with pytest.raises(DomainConfigInvalidError) as put_exc:
        write_domain_config(
            SETTINGS,
            WORKSPACE_ID,
            _payload(read_domain_config(SETTINGS, WORKSPACE_ID), []),
        )
    assert "未知表" not in put_exc.value.details["reason"]


def test_empty_candidate_list_is_ok(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["other.public.users.md"])
    assert list_domain_tables(SETTINGS, WORKSPACE_ID) == {"tables": []}


# ---- 准入：工作区归属 + 唯一成功采集 marker ----


@pytest.mark.parametrize(
    "marker,match",
    [
        (None, "没有 Domain 采集 marker"),
        ("failed", "Domain 采集未成功"),
    ],
)
def test_marker_missing_or_failed_is_404(tmp_path, monkeypatch, marker, match):
    root = _install(monkeypatch, tmp_path, marker=marker)
    _write_yaml(root, SAMPLE)
    with pytest.raises(DomainConfigOutputMissingError, match=match):
        read_domain_config(SETTINGS, WORKSPACE_ID)
    with pytest.raises(DomainConfigOutputMissingError, match=match):
        list_domain_tables(SETTINGS, WORKSPACE_ID)
    with pytest.raises(DomainConfigOutputMissingError, match=match):
        write_domain_config(SETTINGS, WORKSPACE_ID, {"fingerprint": "x"})


def test_multiple_markers_is_404(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path, marker=None)
    _write_yaml(root, SAMPLE)
    domain_dir = root / "domain"
    # write_collection_marker 写入前会清旧 marker，这里手工落两个模拟异常状态
    from backend.workspace_files.timestamps import marker_timestamp, shanghai_now

    stamp = marker_timestamp(shanghai_now())
    other = UUID("99999999-9999-4999-8999-999999999999")
    (domain_dir / f".coll_run_id_{RUN_ID}_{stamp}_success").touch()
    (domain_dir / f".coll_run_id_{other}_{stamp}_failed").touch()
    assert len(list_collection_markers(domain_dir)) == 2
    with pytest.raises(DomainConfigOutputMissingError, match="多个采集 marker"):
        read_domain_config(SETTINGS, WORKSPACE_ID)


def test_workspace_or_source_missing_or_disabled(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path, FakeSession(None, _source_row()))
    _write_yaml(root, SAMPLE)
    with pytest.raises(DomainConfigOutputMissingError, match="工作区不存在"):
        read_domain_config(SETTINGS, WORKSPACE_ID)

    _install(monkeypatch, tmp_path, FakeSession(_workspace_row(), None))
    with pytest.raises(MetaLoadSourceNotFoundError):
        read_domain_config(SETTINGS, WORKSPACE_ID)

    _install(
        monkeypatch,
        tmp_path,
        FakeSession(_workspace_row(), _source_row(is_active=False)),
    )
    with pytest.raises(MetaLoadSourceDisabledError):
        write_domain_config(SETTINGS, WORKSPACE_ID, {"fingerprint": "x"})


# ---- PUT：往返、校验、并发 ----


def test_put_roundtrip_preserves_description_and_block_style(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
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
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
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
    saved = write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, domains))
    assert [item["name"] for item in saved["domains"]] == ["订单", "用户管理"]
    assert saved["database"]["description"] == "库描述原文"
    text = (root / "domain" / "db_domains.yaml").read_text(encoding="utf-8")
    assert "description: |" in text
    assert "db_desc" not in text
    again = read_domain_config(SETTINGS, WORKSPACE_ID)
    assert again["domains"] == saved["domains"]
    assert again["fingerprint"] == saved["fingerprint"]


@pytest.mark.parametrize(
    "mutate,reason_part",
    [
        (lambda domains: domains + [domains[0]], "重复"),
        (
            lambda domains: [{**domains[0], "tables": ["orders.public.missing"]}],
            "未知表",
        ),
        (lambda domains: [{**domains[0], "name": "  "}], "名称"),
        (lambda domains: [{**domains[0], "description": ""}], "描述"),
    ],
)
def test_put_rejects_without_writing(tmp_path, monkeypatch, mutate, reason_part):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    payload = _payload(view, mutate(view["domains"]))
    with pytest.raises(DomainConfigInvalidError) as exc:
        write_domain_config(SETTINGS, WORKSPACE_ID, payload)
    assert reason_part in exc.value.details["reason"]
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before


def test_put_rejects_renamed_database_fields(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    renamed = _payload(view, view["domains"])
    renamed["database"] = {
        "name": "other",
        "description": view["database"]["description"],
    }
    with pytest.raises(DomainConfigInvalidError):
        write_domain_config(SETTINGS, WORKSPACE_ID, renamed)
    changed = _payload(view, view["domains"])
    changed["database"] = {"name": DB_NAME, "description": "改过"}
    with pytest.raises(DomainConfigInvalidError) as exc:
        write_domain_config(SETTINGS, WORKSPACE_ID, changed)
    assert "库描述" in exc.value.details["reason"]
    stale = _payload(view, view["domains"])
    stale["fingerprint"] = "0" * 64
    with pytest.raises(DomainConfigRegeneratedError):
        write_domain_config(SETTINGS, WORKSPACE_ID, stale)
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before


def test_put_unchanged_does_not_rewrite_and_skips_backup(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    saved = write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, view["domains"]))
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before
    assert saved["fingerprint"] == view["fingerprint"]
    # 实质未变 → 原样返回，不进入备份与写回
    assert not (root / "domain" / ".backup").exists()


@pytest.mark.parametrize("matching_fingerprint", [False, True])
def test_put_checks_fingerprint_before_parsing_yaml(
    tmp_path, monkeypatch, matching_fingerprint
):
    root = _install(monkeypatch, tmp_path)
    raw = b": [\n"
    path = root / "domain" / "db_domains.yaml"
    path.write_bytes(raw)
    fingerprint = hashlib.sha256(raw).hexdigest() if matching_fingerprint else "stale"
    error = (
        DomainConfigInvalidError
        if matching_fingerprint
        else DomainConfigRegeneratedError
    )

    with pytest.raises(error):
        write_domain_config(SETTINGS, WORKSPACE_ID, {"fingerprint": fingerprint})

    assert path.read_bytes() == raw


def test_put_strips_domain_whitespace(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    saved = write_domain_config(
        SETTINGS,
        WORKSPACE_ID,
        _payload(
            view,
            [
                {
                    "name": "  用户管理  ",
                    "description": "  用户  ",
                    "tables": ["orders.public.users"],
                }
            ],
        ),
    )
    assert saved["domains"] == [
        {"name": "用户管理", "description": "用户", "tables": ["orders.public.users"]}
    ]


def test_same_table_in_two_domains_and_empty_tables(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    saved = write_domain_config(
        SETTINGS,
        WORKSPACE_ID,
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
    from datetime import datetime, timezone

    registration = RegistrationSnapshot(
        datetime(2026, 10, 9, tzinfo=timezone.utc), DB_NAME, ("public",)
    )
    import backend.profile_loading.domain_loader as loader

    monkeypatch.setattr(
        loader, "resolve_domain_output_dir", lambda s, w: root / "domain"
    )
    monkeypatch.setattr(loader, "resolve_md_output_dir", lambda s, w: root / "md")
    rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration)
    assert len(rows) == 2
    assert {row["domain_name"] for row in rows} == {"甲", "乙"}


# ---- 文件级备份（doc 6.2 §5.2、A28/A30） ----


def test_put_success_backs_up_only_yaml_and_keeps_marker(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    marker_names = [p.name for p in list_collection_markers(root / "domain")]

    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    changed = [{**view["domains"][0], "description": "新描述"}]
    write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, changed))

    backup_root = root / "domain" / ".backup"
    backups = sorted(backup_root.iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("bak_")
    # 只复制将被覆盖的 db_domains.yaml；marker 与其他产物不进备份
    assert [p.name for p in sorted(backups[0].iterdir())] == ["db_domains.yaml"]
    assert (backups[0] / "db_domains.yaml").read_bytes() == before
    # 顶层 marker 原位未动
    assert [p.name for p in list_collection_markers(root / "domain")] == marker_names


def test_put_backup_failure_aborts_save_without_touching_anything(
    tmp_path, monkeypatch
):
    """A28：备份失败 → 保存终止，原文件与 marker 不动，无备份目录与临时文件。"""
    import backend.profile_loading.domain_config_api as api

    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    marker_names = [p.name for p in list_collection_markers(root / "domain")]
    calls = []

    def failing_backup(directory, relative_paths, **kwargs):
        calls.append((Path(directory), list(relative_paths)))
        raise EditBackupFailedError("注入的备份失败")

    monkeypatch.setattr(api, "backup_files_before_edit", failing_backup)
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    changed = [{**view["domains"][0], "description": "备份失败场景"}]
    with pytest.raises(DomainConfigBackupFailedError):
        write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, changed))

    assert calls == [(root / "domain", ["db_domains.yaml"])]
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before
    assert [p.name for p in list_collection_markers(root / "domain")] == marker_names
    assert not (root / "domain" / ".backup").exists()
    assert list((root / "domain").glob(".tmp-domain-*")) == []


def test_replace_failure_keeps_original_and_backup(tmp_path, monkeypatch):
    """A30 服务层断言：替换失败原文件不动，已完成的文件级备份保留。"""
    import backend.profile_loading.domain_config_api as api

    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    before = (root / "domain" / "db_domains.yaml").read_bytes()

    def _boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(api, "replace_domain_file", _boom)
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    changed = [{**view["domains"][0], "description": "已修改描述"}]
    with pytest.raises(DomainConfigWriteFailedError):
        write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, changed))
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before
    assert list((root / "domain").glob(".tmp-domain-*")) == []
    # 已完成的备份保留供人工核对
    backup_root = root / "domain" / ".backup"
    backups = list(backup_root.iterdir())
    assert len(backups) == 1
    assert (backups[0] / "db_domains.yaml").read_bytes() == before


def test_put_locks_workspace_row_before_fingerprint_read_and_replace(
    tmp_path, monkeypatch
):
    """锁序核对：工作区行锁（doc 6.2 §5.1）先于指纹读取与原子替换。"""
    import backend.profile_loading.domain_config_api as api

    session = _session()
    root = _install(monkeypatch, tmp_path, session)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])

    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    original_read = api._read_regular_bytes
    original_replace = api.replace_domain_file

    def read(path):
        session.events.append("read")
        return original_read(path)

    def replace(src, dst):
        session.events.append("replace")
        original_replace(src, dst)

    monkeypatch.setattr(api, "_read_regular_bytes", read)
    monkeypatch.setattr(api, "replace_domain_file", replace)
    changed = [{**view["domains"][0], "description": "新描述"}]
    write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, changed))
    # 替换前只读一次，用于核对指纹并解析；替换后保留一次核验读取。
    assert session.events == ["lock", "read", "replace", "read"]


def test_empty_domains_fail_verify_artifact(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    view = read_domain_config(SETTINGS, WORKSPACE_ID)
    write_domain_config(SETTINGS, WORKSPACE_ID, _payload(view, []))
    ok, reason = verify_domain_artifact(root / "domain" / "db_domains.yaml")
    assert ok is False
    assert "domains 为空" in reason
