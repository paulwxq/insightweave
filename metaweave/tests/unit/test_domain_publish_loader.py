"""受管 Domain 发布加载器（doc 21.2 §4）：加固读取、解析规则与 MD 清单校验。

断代改造后按 (source_id, workspace_id, registration) 定位；旧的
`inspect_domain_context`（job_info 行检查）已删除，准入统一走目录 marker。
"""

import json
import os
from datetime import datetime, timezone

import pytest
import yaml

from backend.profile_loading.domain_loader import (
    DomainPublishInputError,
    check_domain_output_ready,
    read_domain_rows,
)
from backend.profile_loading.json_loader import RegistrationSnapshot
from tests.unit.test_publish_service import (
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    step_dir,
)

DB_NAME = "orders"
RUN_MARKER = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)


def registration(**overrides):
    values = dict(updated_at=RUN_MARKER, db_name=DB_NAME, selected_schemas=("public",))
    values.update(overrides)
    return RegistrationSnapshot(**values)


def sample_payload():
    return {
        "database": {"name": DB_NAME, "description": "库说明"},
        "llm_inference": {"max_domains_per_table": 3},
        "domains": [
            {
                "name": "_未分类_",
                "description": "无法归入其他业务主题的表",
                "tables": [],
            },
            {
                "name": "用户管理",
                "description": "负责用户基础信息",
                "tables": ["orders.public.users", "orders.public.products"],
            },
            {
                "name": "订单交易",
                "description": "负责订单交易",
                "tables": ["orders.public.users"],
            },
        ],
    }


SAMPLE_MD_FILES = (
    "orders.public.users.md",
    "orders.public.products.md",
)


def write_yaml(tmp_path, payload="default", text=None):
    domain_dir = step_dir(tmp_path, "domain")
    domain_dir.mkdir(parents=True, exist_ok=True)
    if text is None:
        if payload == "default":
            payload = sample_payload()
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    (domain_dir / "db_domains.yaml").write_text(text, encoding="utf-8")
    return domain_dir


def write_md(tmp_path, names=SAMPLE_MD_FILES, manifest_files="default", manifest=True):
    md_dir = step_dir(tmp_path, "md")
    md_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (md_dir / name).write_text("# 表\n", encoding="utf-8")
    if manifest:
        files = list(names) if manifest_files == "default" else manifest_files
        (md_dir / "manifest.json").write_text(
            json.dumps({"files": files}), encoding="utf-8"
        )
    return md_dir


@pytest.fixture(autouse=True)
def project_root(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


class TestCheckOutputReady:
    def test_ready(self, tmp_path):
        write_yaml(tmp_path)
        assert check_domain_output_ready(SOURCE_ID, WORKSPACE_ID) is True

    def test_yaml_missing(self, tmp_path):
        step_dir(tmp_path, "domain").mkdir(parents=True)
        assert check_domain_output_ready(SOURCE_ID, WORKSPACE_ID) is False

    def test_yaml_symlink_rejected(self, tmp_path):
        domain_dir = write_yaml(tmp_path)
        target = domain_dir / "db_domains.yaml"
        real = tmp_path / "real.yaml"
        real.write_text("database: {name: orders}\ndomains: []\n", encoding="utf-8")
        target.unlink()
        target.symlink_to(real)
        assert check_domain_output_ready(SOURCE_ID, WORKSPACE_ID) is False

    def test_directory_symlink_rejected(self, tmp_path):
        _, _md_dir = (step_dir(tmp_path, "domain"), step_dir(tmp_path, "md"))
        outside = tmp_path / "outside_domain"
        outside.mkdir(parents=True)
        (outside / "db_domains.yaml").write_text("{}", encoding="utf-8")
        domain_dir = step_dir(tmp_path, "domain")
        domain_dir.parent.mkdir(parents=True, exist_ok=True)
        domain_dir.symlink_to(outside)
        assert check_domain_output_ready(SOURCE_ID, WORKSPACE_ID) is False

    def test_yaml_fifo_rejected(self, tmp_path):
        domain_dir = step_dir(tmp_path, "domain")
        domain_dir.mkdir(parents=True)
        os.mkfifo(domain_dir / "db_domains.yaml")
        assert check_domain_output_ready(SOURCE_ID, WORKSPACE_ID) is False


class TestReadDomainRows:
    def test_valid_mapping_rows(self, tmp_path):
        write_yaml(tmp_path)
        write_md(tmp_path)
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        # 行序按文件顺序（确定性），一张表属于多个 domain 时留多行
        assert rows == [
            {
                "schema_name": "public",
                "table_name": "users",
                "domain_name": "用户管理",
                "domain_desc": "负责用户基础信息",
                "db_name": DB_NAME,
            },
            {
                "schema_name": "public",
                "table_name": "products",
                "domain_name": "用户管理",
                "domain_desc": "负责用户基础信息",
                "db_name": DB_NAME,
            },
            {
                "schema_name": "public",
                "table_name": "users",
                "domain_name": "订单交易",
                "domain_desc": "负责订单交易",
                "db_name": DB_NAME,
            },
        ]

    def test_zero_rows_when_all_tables_empty(self, tmp_path):
        payload = sample_payload()
        payload["domains"] = [payload["domains"][0]]  # 只有空的 _未分类_
        write_yaml(tmp_path, payload)
        write_md(tmp_path)
        assert read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration()) == []

    def test_strip_applied_to_segments_and_texts(self, tmp_path):
        payload = {
            "database": {"name": " orders "},
            "domains": [
                {
                    "name": " 用户管理 ",
                    "description": " 用户域 ",
                    "tables": ["orders.public. users "],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path, names=("orders.public.users.md",))
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        assert rows == [
            {
                "schema_name": "public",
                "table_name": "users",
                "domain_name": "用户管理",
                "domain_desc": "用户域",
                "db_name": DB_NAME,
            }
        ]

    def test_inner_spaces_preserved(self, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "用户管理",
                    "description": "域",
                    "tables": ["orders.public.my table"],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path, names=("orders.public.my table.md",))
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        assert rows[0]["table_name"] == "my table"

    def test_duplicate_triple_kept_once(self, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "用户管理",
                    "description": "域",
                    "tables": ["orders.public.users", "orders.public.users"],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path, names=("orders.public.users.md",))
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        assert len(rows) == 1

    def test_same_name_same_description_merges(self, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "用户管理",
                    "description": "域",
                    "tables": ["orders.public.users"],
                },
                {
                    "name": "用户管理",
                    "description": " 域 ",
                    "tables": ["orders.public.products"],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path)
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        assert {(row["table_name"], row["domain_name"]) for row in rows} == {
            ("users", "用户管理"),
            ("products", "用户管理"),
        }

    def test_conflicting_description_fails_even_for_empty_tables(self, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {"name": "用户管理", "description": "A", "tables": []},
                {
                    "name": "用户管理",
                    "description": "B",
                    "tables": ["orders.public.users"],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_top_level_not_object(self, tmp_path):
        write_yaml(tmp_path, text="- a\n- b\n")
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_domains_not_list(self, tmp_path):
        write_yaml(tmp_path, {"database": {"name": DB_NAME}, "domains": {}})
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_database_missing_or_not_object(self, tmp_path):
        write_yaml(tmp_path, {"domains": []})
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

        write_yaml(tmp_path, {"database": "orders", "domains": []})
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_database_name_blank(self, tmp_path):
        write_yaml(tmp_path, {"database": {"name": "   "}, "domains": []})
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_database_name_mismatch(self, tmp_path):
        write_yaml(tmp_path, {"database": {"name": "otherdb"}, "domains": []})
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_domain_item_not_object(self, tmp_path):
        write_yaml(tmp_path, {"database": {"name": DB_NAME}, "domains": ["用户管理"]})
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    @pytest.mark.parametrize(
        "item",
        [
            {"name": "  ", "description": "域", "tables": []},
            {"name": 5, "description": "域", "tables": []},
            {"name": "用户管理", "description": "", "tables": []},
            {"name": "用户管理", "description": None, "tables": []},
        ],
    )
    def test_domain_name_or_description_invalid(self, tmp_path, item):
        write_yaml(tmp_path, {"database": {"name": DB_NAME}, "domains": [item]})
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_tables_not_list_or_entry_not_string(self, tmp_path):
        write_yaml(
            tmp_path,
            {
                "database": {"name": DB_NAME},
                "domains": [
                    {"name": "域", "description": "d", "tables": "orders.public.users"}
                ],
            },
        )
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

        write_yaml(
            tmp_path,
            {
                "database": {"name": DB_NAME},
                "domains": [{"name": "域", "description": "d", "tables": [5]}],
            },
        )
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    @pytest.mark.parametrize(
        "name",
        [
            "orders.users",  # 两段
            "orders.public.a.b",  # 四段
            "orders..users",  # 空段
            "orders.public.   ",  # strip 后空段
        ],
    )
    def test_table_name_shape_invalid(self, tmp_path, name):
        write_yaml(
            tmp_path,
            {
                "database": {"name": DB_NAME},
                "domains": [{"name": "域", "description": "d", "tables": [name]}],
            },
        )
        write_md(tmp_path, names=())
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_first_segment_must_match_db(self, tmp_path):
        write_yaml(
            tmp_path,
            {
                "database": {"name": DB_NAME},
                "domains": [
                    {
                        "name": "域",
                        "description": "d",
                        "tables": ["otherdb.public.users"],
                    }
                ],
            },
        )
        write_md(tmp_path, names=("otherdb.public.users.md",))
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_schema_outside_selected_schemas(self, tmp_path):
        write_yaml(
            tmp_path,
            {
                "database": {"name": DB_NAME},
                "domains": [
                    {"name": "域", "description": "d", "tables": ["orders.sales.users"]}
                ],
            },
        )
        write_md(tmp_path, names=("orders.sales.users.md",))
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_yaml_symlink_rejected(self, tmp_path):
        domain_dir = step_dir(tmp_path, "domain")
        domain_dir.mkdir(parents=True)
        real = tmp_path / "real.yaml"
        real.write_text(
            yaml.safe_dump(sample_payload(), allow_unicode=True), encoding="utf-8"
        )
        (domain_dir / "db_domains.yaml").symlink_to(real)
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_yaml_fifo_rejected(self, tmp_path):
        domain_dir = step_dir(tmp_path, "domain")
        domain_dir.mkdir(parents=True)
        os.mkfifo(domain_dir / "db_domains.yaml")
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_yaml_unparsable(self, tmp_path):
        write_yaml(tmp_path, text="domains: [\n")
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_manifest_missing_fails_even_with_zero_rows(self, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [{"name": "_未分类_", "description": "d", "tables": []}],
        }
        write_yaml(tmp_path, payload)
        md_dir = write_md(tmp_path, manifest=False)
        assert md_dir.exists()
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_manifest_shape_invalid(self, tmp_path):
        write_yaml(tmp_path)
        write_md(tmp_path, manifest_files=[])
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

        write_md(tmp_path, manifest_files=["orders.public.users.txt"])
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

        write_md(tmp_path, manifest_files="orders.public.users.md")
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_manifest_symlink_rejected(self, tmp_path):
        write_yaml(tmp_path)
        md_dir = write_md(tmp_path)
        manifest = md_dir / "manifest.json"
        real = tmp_path / "real_manifest.json"
        real.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        manifest.unlink()
        manifest.symlink_to(real)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_table_not_in_manifest(self, tmp_path):
        write_yaml(tmp_path)
        write_md(tmp_path, names=("orders.public.users.md",))
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_manifest_listed_file_missing_on_disk(self, tmp_path):
        write_yaml(tmp_path)
        write_md(tmp_path)
        (step_dir(tmp_path, "md") / "orders.public.products.md").unlink()
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_manifest_listed_file_symlink_rejected(self, tmp_path):
        write_yaml(tmp_path)
        md_dir = write_md(tmp_path)
        target = md_dir / "orders.public.products.md"
        real = tmp_path / "real.md"
        real.write_text("# 表\n", encoding="utf-8")
        target.unlink()
        target.symlink_to(real)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_directory_symlink_rejected(self, tmp_path):
        outside = tmp_path / "outside_domain"
        outside.mkdir(parents=True)
        (outside / "db_domains.yaml").write_text(
            yaml.safe_dump(sample_payload(), allow_unicode=True), encoding="utf-8"
        )
        domain_dir = step_dir(tmp_path, "domain")
        domain_dir.parent.mkdir(parents=True, exist_ok=True)
        domain_dir.symlink_to(outside)
        write_md(tmp_path)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())


def test_manifest_name_matches_collection_side():
    """清单文件名与采集侧 md 步骤常量一致（测试锁定，同 ddl_loader 先例）。"""
    from backend.data_sources.md_step_service import MD_MANIFEST_NAME as COLLECTION_NAME
    from backend.profile_loading.domain_loader import MD_MANIFEST_NAME

    assert MD_MANIFEST_NAME == COLLECTION_NAME


class TestTablesFieldBoundary:
    def test_tables_field_missing_skips_domain(self, tmp_path):
        """缺少 tables 字段按"缺失"跳过（doc 21.2 §4 规则 4）。"""
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {"name": "_未分类_", "description": "无法归入其他业务主题的表"},
                {
                    "name": "用户管理",
                    "description": "域",
                    "tables": ["orders.public.users"],
                },
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path, names=("orders.public.users.md",))
        rows = read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
        assert [(row["table_name"], row["domain_name"]) for row in rows] == [
            ("users", "用户管理")
        ]

    def test_tables_explicit_null_fails_even_with_valid_domain(self, tmp_path):
        """显式 tables: null 不是列表，整次失败；不能静默丢掉该 domain 的旧映射。"""
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "用户管理",
                    "description": "域",
                    "tables": ["orders.public.users"],
                },
                {"name": "订单交易", "description": "域2", "tables": None},
            ],
        }
        write_yaml(tmp_path, payload)
        write_md(tmp_path, names=("orders.public.users.md",))
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())


class TestDirectoryResolutionErrors:
    def test_domain_dir_resolution_oserror_is_parse_failure(
        self, tmp_path, monkeypatch
    ):
        """目录访问抛 OSError 时按解析失败处理（doc 21.2 §4 规则 2）。"""
        from backend.profile_loading import domain_loader

        write_yaml(tmp_path)
        write_md(tmp_path)

        def boom(source_id, workspace_id):
            raise PermissionError("denied")

        monkeypatch.setattr(domain_loader, "resolve_domain_output_dir", boom)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())

    def test_md_dir_resolution_oserror_is_parse_failure(self, tmp_path, monkeypatch):
        from backend.profile_loading import domain_loader

        write_yaml(tmp_path)
        write_md(tmp_path)

        def boom(source_id, workspace_id):
            raise PermissionError("denied")

        monkeypatch.setattr(domain_loader, "resolve_md_output_dir", boom)
        with pytest.raises(DomainPublishInputError):
            read_domain_rows(SOURCE_ID, WORKSPACE_ID, registration())
