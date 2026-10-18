"""doc 4 多源键空间与来源绑定单元测试

覆盖：
- 键空间统一判定式（db 前缀允许内部空格/连字符/数字开头；拒绝点号、
  首尾空白、控制字符）与两段/三段输入一致性；
- 知识前缀不进入 SQL 表引用（display 剥 db 前缀）；
- ``apply_source_runtime_config`` 按源注入三键并拒绝非法库名；
- Runner 的会话/请求/服务三方来源核对（不一致拒绝、首轮持久化绑定）；
- 工具参数模式不含 source_id（模型不可填写）。
"""

from __future__ import annotations

import pytest

from src.modules.db_deepagent.config import (
    DbAgentConfigError,
    apply_source_runtime_config,
)
from src.modules.db_deepagent.errors import InvalidTableIdError
from src.modules.db_deepagent.runner import _resolve_source_binding
from src.tests.db_deepagent.conftest import (
    TEST_SOURCE_ID,
    TEST_SOURCE_ID_B,
    FakeBusinessDb,
    FakeMilvus,
)
from src.modules.db_deepagent.services.table_catalog import TableCatalog

pytestmark = pytest.mark.unit

# 双方共用的判定式样例（doc 4 §4.2：MetaWeave 与 NL2SQL 各自实现、同组样例）
VALID_DB_PREFIXES = ["my orders", "my-orders", "2026orders", "Orders", "a"]
INVALID_DB_PREFIXES = ["", " orders", "orders ", "my.orders", "order\t s"]


def _catalog(cfg, prefix: str) -> TableCatalog:
    updated = cfg.model_copy(deep=True)
    updated.business_db.table_key_prefix = prefix
    return TableCatalog(updated, FakeMilvus(), FakeBusinessDb([]))


class TestDbPrefixKeyspace:
    def test_hyphenated_prefix_two_segment_input(self, db_agent_config):
        """`my-orders` 源：`public.orders` → `my-orders.public.orders`。"""
        catalog = _catalog(db_agent_config, "my-orders")
        key = catalog.normalize_table_id("public.orders")
        assert key.key == "my-orders.public.orders"
        assert key.display == "public.orders"  # 知识前缀不进 SQL 表引用

    def test_leading_digit_prefix(self, db_agent_config):
        catalog = _catalog(db_agent_config, "2026orders")
        key = catalog.normalize_table_id("public.orders")
        assert key.key == "2026orders.public.orders"
        assert key.display == "public.orders"

    def test_internal_space_prefix(self, db_agent_config):
        catalog = _catalog(db_agent_config, "my orders")
        key = catalog.normalize_table_id("public.orders")
        assert key.key == "my orders.public.orders"
        assert key.display == "public.orders"

    @pytest.mark.parametrize("prefix", INVALID_DB_PREFIXES)
    def test_invalid_prefix_rejected(self, db_agent_config, prefix):
        catalog = _catalog(db_agent_config, prefix)
        with pytest.raises(InvalidTableIdError):
            catalog.normalize_table_id("public.orders")

    def test_three_segment_must_match_prefix(self, db_agent_config):
        catalog = _catalog(db_agent_config, "my-orders")
        key = catalog.normalize_table_id("my-orders.public.orders")
        assert key.key == "my-orders.public.orders"
        with pytest.raises(InvalidTableIdError, match="首段"):
            catalog.normalize_table_id("orders.public.orders")

    def test_hyphenated_schema_rejected(self, db_agent_config):
        """schema/表保持普通标识符范围：连字符 schema 明确拒绝。"""
        catalog = _catalog(db_agent_config, "orders")
        with pytest.raises(InvalidTableIdError):
            catalog.normalize_table_id("my-schema.orders")

    def test_dotted_db_name_rejected_at_normalize(self, db_agent_config):
        """含点号库名无法承载三段键：拒绝并报出规则。"""
        catalog = _catalog(db_agent_config, "my.orders")
        with pytest.raises(InvalidTableIdError, match="点号"):
            catalog.normalize_table_id("public.orders")

    def test_display_never_contains_prefix_or_source(self, db_agent_config):
        catalog = _catalog(db_agent_config, "my-orders")
        key = catalog.normalize_table_id("public.orders")
        assert "my-orders" not in key.display
        assert TEST_SOURCE_ID not in key.display


class TestAllowedSchemasScope:
    """doc 4 §4.7：登记 selected_schemas 约束表键归一入口。"""

    def _catalog(self, db_agent_config, allowed):
        cfg = db_agent_config.model_copy(deep=True)
        cfg.business_db.allowed_schemas = allowed
        return TableCatalog(cfg, FakeMilvus(), FakeBusinessDb([]))

    def test_in_scope_schema_passes(self, db_agent_config):
        catalog = self._catalog(db_agent_config, ["public", "sales"])
        assert catalog.normalize_table_id("sales.orders").key == "orders.sales.orders"

    def test_out_of_scope_schema_rejected_two_segment(self, db_agent_config):
        catalog = self._catalog(db_agent_config, ["public"])
        with pytest.raises(InvalidTableIdError, match="selected_schemas"):
            catalog.normalize_table_id("sales.orders")

    def test_out_of_scope_schema_rejected_three_segment(self, db_agent_config):
        catalog = self._catalog(db_agent_config, ["public"])
        with pytest.raises(InvalidTableIdError, match="selected_schemas"):
            catalog.normalize_table_id("orders.sales.orders")

    def test_none_does_not_restrict(self, db_agent_config):
        catalog = self._catalog(db_agent_config, None)
        assert catalog.normalize_table_id("sales.orders").key == "orders.sales.orders"

    def test_unquoted_schema_folds_lower_for_comparison(self, db_agent_config):
        """PG 规则：未加引号的 PUBLIC 折叠 public，与登记名精确匹配放行。"""
        catalog = self._catalog(db_agent_config, ["public"])
        assert catalog.normalize_table_id("PUBLIC.orders").key == "orders.public.orders"

    def test_case_sensitive_registration_not_matched_by_folded(self, db_agent_config):
        """登记 "Sales"（大小写敏感）：未引号 sales 折叠后不匹配，拒绝。"""
        catalog = self._catalog(db_agent_config, ["Sales"])
        with pytest.raises(InvalidTableIdError, match="selected_schemas"):
            catalog.normalize_table_id("sales.orders")

    def test_exact_match_wins_and_preserves_case(self, db_agent_config):
        """知识键优先按真实名称精确匹配：`orders.Sales.orders` 直接通过，
        三段键保留 Sales 原样（doc 4 §4.2 不改写大小写）。"""
        catalog = self._catalog(db_agent_config, ["Sales"])
        key = catalog.normalize_table_id("orders.Sales.orders")
        assert key.key == "orders.Sales.orders"

    def test_dual_registration_no_cross_normalization(self, db_agent_config):
        """同时登记 Sales 与 sales：各自精确归一，折叠兜底不互相吞并。"""
        catalog = self._catalog(db_agent_config, ["Sales", "sales"])
        assert catalog.normalize_table_id("Sales.orders").key == "orders.Sales.orders"
        assert catalog.normalize_table_id("sales.orders").key == "orders.sales.orders"
        # 未引号大写折叠兜底命中小写登记名
        assert catalog.normalize_table_id("SALES.orders").key == "orders.sales.orders"


class TestApplySourceRuntimeConfig:
    def test_three_keys_injected_from_registration(self, db_agent_config):
        updated = apply_source_runtime_config(db_agent_config, "my-orders")
        assert updated.business_db.database == "my-orders"
        assert updated.business_db.expected_database == "my-orders"
        assert updated.business_db.table_key_prefix == "my-orders"
        # 原配置对象不被修改
        assert db_agent_config.business_db.table_key_prefix == "orders"

    @pytest.mark.parametrize("bad", INVALID_DB_PREFIXES)
    def test_invalid_db_name_rejected(self, db_agent_config, bad):
        with pytest.raises(DbAgentConfigError):
            apply_source_runtime_config(db_agent_config, bad)

    def test_inconsistent_yaml_config_rejected(self, db_agent_config):
        """三键不一致的配置在检查表阶段拒绝（不得沿用固定前缀）。"""
        broken = db_agent_config.model_copy(deep=True)
        broken.business_db.table_key_prefix = "other"
        from src.modules.db_deepagent.config import run_config_checks

        with pytest.raises(DbAgentConfigError, match="一致"):
            run_config_checks(broken, check_embedding_profile=False)


class TestRunnerSourceBinding:
    def test_first_run_binds_service_source(self):
        binding = _resolve_source_binding(None, None, TEST_SOURCE_ID)
        assert binding == TEST_SOURCE_ID

    def test_request_matches_service_ok(self):
        binding = _resolve_source_binding(
            {"source_id": TEST_SOURCE_ID}, TEST_SOURCE_ID, TEST_SOURCE_ID
        )
        assert binding == TEST_SOURCE_ID

    def test_request_conflict_with_service_rejected(self):
        from src.modules.db_deepagent.runner import _RejectedSession

        with pytest.raises(_RejectedSession, match="不一致"):
            _resolve_source_binding(None, TEST_SOURCE_ID_B, TEST_SOURCE_ID)

    def test_session_bound_to_other_source_rejected(self):
        """A 源会话 + B 源服务绑定：拒绝（防串源），提示新建会话。"""
        from src.modules.db_deepagent.runner import _RejectedSession

        with pytest.raises(_RejectedSession, match="其他 source_id"):
            _resolve_source_binding(
                {"source_id": TEST_SOURCE_ID_B}, None, TEST_SOURCE_ID
            )

    def test_invalid_request_source_rejected(self):
        from src.modules.db_deepagent.runner import _RejectedSession

        with pytest.raises(_RejectedSession, match="UUID"):
            _resolve_source_binding(None, "not-a-uuid", TEST_SOURCE_ID)

    def test_missing_service_binding_rejected(self):
        from src.modules.db_deepagent.runner import _RejectedSession

        with pytest.raises(_RejectedSession, match="未绑定"):
            _resolve_source_binding(None, None, None)

    def test_legacy_session_without_binding_rejected(self):
        """旧会话（记录存在但缺来源绑定）不被新源接管：拒绝续聊并提示
        新建会话——否则旧 checkpoint 上下文进入新源会话（doc 4 §5.2）。"""
        from src.modules.db_deepagent.runner import _RejectedSession

        legacy_record = {"current_thread_id": "dbda:s1:1", "thread_seq": 1}
        with pytest.raises(_RejectedSession, match="缺少来源绑定"):
            _resolve_source_binding(legacy_record, None, TEST_SOURCE_ID_B)
        with pytest.raises(_RejectedSession, match="新建会话"):
            _resolve_source_binding(legacy_record, TEST_SOURCE_ID_B, TEST_SOURCE_ID_B)

    def test_fresh_session_without_record_binds(self):
        """对照：无会话记录（首次创建）正常绑定服务来源。"""
        binding = _resolve_source_binding(None, None, TEST_SOURCE_ID_B)
        assert binding == TEST_SOURCE_ID_B


class TestToolSchemaHasNoSourceId:
    @pytest.fixture()
    def all_tools(self, guard_services) -> list:
        from src.modules.db_deepagent.tools import build_business_tools
        from src.modules.db_deepagent.tools.final_answer import (
            make_submit_final_answer_tool,
        )

        tools = list(build_business_tools(guard_services))
        tools.append(make_submit_final_answer_tool())
        return tools

    def test_no_source_id_in_any_visible_schema(self, all_tools):
        """source_id 不作为模型可填写的工具参数（doc 4 §4.7）。"""
        for tool in all_tools:
            schema = tool.tool_call_schema.model_json_schema()
            props = schema.get("properties") or {}
            assert "source_id" not in props, tool.name
            assert "entity_id" not in props, tool.name


class TestSessionBindingPersistence:
    def test_session_record_roundtrips_source_id(self):
        """doc 4 §5.2：session_record 带 source_id，upsert 整体覆盖后可读回。"""
        from datetime import datetime

        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.tests.db_deepagent.conftest import FakeStore

        archive = QaArchive(FakeStore())
        record = archive.session_record(
            session_id="s-bind",
            thread_id="dbda:s-bind:1",
            thread_seq=1,
            created_at=datetime(2026, 10, 17, 12, 0),
            last_active_at=datetime(2026, 10, 17, 12, 0),
            active=False,
            source_id=TEST_SOURCE_ID,
        )
        archive.upsert_session("s-bind", record)
        assert archive.get_session("s-bind")["source_id"] == TEST_SOURCE_ID

    def test_missing_binding_written_as_none_is_preserved(self):
        """旧记录无绑定时 Runner 侧保留 None 不覆盖（_write_thread_tracks 语义）。"""
        from datetime import datetime

        from src.modules.db_deepagent.persistence.archive import QaArchive
        from src.tests.db_deepagent.conftest import FakeStore

        archive = QaArchive(FakeStore())
        record = archive.session_record(
            session_id="s-old",
            thread_id="dbda:s-old:1",
            thread_seq=1,
            created_at=datetime(2026, 10, 1, 12, 0),
            last_active_at=datetime(2026, 10, 1, 12, 0),
            active=False,
        )
        archive.upsert_session("s-old", record)
        assert "source_id" in archive.get_session("s-old")
