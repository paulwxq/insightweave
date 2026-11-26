"""装配验证单元测试（docs/v4/5 §10.6、§18.2 P12）

13 件工具（步骤 4 口径）、DeltaChannel、无 files 通道、submit_final_answer
设置、可见 Schema 字段契约、错误 harness 键拒绝。
"""

from __future__ import annotations

import pytest
from langgraph.channels import DeltaChannel

from src.modules.db_deepagent.agent import (
    STEP4_TOOLS,
    TOOL_VISIBLE_FIELDS,
    build_backend,
    build_db_agent,
    build_permissions,
    stage_expected_tool_names,
    verify_assembly,
)
from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.errors import HarnessProfileError
from src.modules.db_deepagent.services import DbAgentServices

pytestmark = pytest.mark.unit


class TestToolSetConstants:
    def test_step4_profile_has_14_tools(self, db_agent_config: DbAgentConfig):
        """doc 6.3 §10：阶段集合同样包含任务工具（13 → 14）。"""
        names = stage_expected_tool_names(db_agent_config, include_result_tools=False)
        assert len(names) == 14
        assert "record_sub_tasks" in names
        assert "read_result" not in names and "analyze_result" not in names
        assert names == set(STEP4_TOOLS)

    def test_step5_default_16_tools(self, db_agent_config: DbAgentConfig):
        """doc 6.3 §10：11 业务 + 提交 + 4 文件 = 16。"""
        names = stage_expected_tool_names(db_agent_config)
        assert len(names) == 16
        assert "record_sub_tasks" in names
        assert {"read_result", "analyze_result"} <= names

    def test_todo_list_adds_write_todos(self, db_agent_config: DbAgentConfig):
        cfg = db_agent_config.model_copy(deep=True)
        cfg.features.todo_list = True
        names = stage_expected_tool_names(cfg)
        assert "write_todos" in names and len(names) == 17

    def test_forbidden_tools_absent(self):
        for name in ("write_file", "edit_file", "delete", "execute", "task"):
            assert name not in STEP4_TOOLS


class TestAssembly:
    def test_assembly_passes_and_verifies(self, assembly_services):
        handle = build_db_agent(assembly_services, session_id="t-assembly")
        expected = verify_assembly(handle.agent, assembly_services.config)
        assert len(expected) == 16
        assert expected == stage_expected_tool_names(assembly_services.config)
        bound = handle.agent.nodes["tools"].bound.tools_by_name
        assert set(bound) == expected
        assert isinstance(handle.agent.channels["messages"], DeltaChannel)
        assert "files" not in handle.agent.channels
        assert bound["submit_final_answer"].return_direct is False

    def test_visible_schema_contract(self, assembly_services):
        handle = build_db_agent(assembly_services, session_id="t-schema")
        bound = handle.agent.nodes["tools"].bound.tools_by_name
        for name, (fields, required) in TOOL_VISIBLE_FIELDS.items():
            schema = bound[name].tool_call_schema.model_json_schema()
            props = set(schema["properties"])
            assert props == set(fields), name
            assert set(required) <= set(schema.get("required") or []), name

    def test_wrong_harness_key_rejected(self, assembly_services):
        cfg = assembly_services.config.model_copy(deep=True)
        cfg.model.expected_harness_key = "openai:wrong-model"
        services = DbAgentServices(**{**assembly_services.__dict__, "config": cfg})
        with pytest.raises(HarnessProfileError):
            build_db_agent(services, session_id="t-harness")


class TestBackend:
    def test_permissions_deny_all_writes_and_hidden(self, db_agent_config, tmp_path):
        permissions = build_permissions(db_agent_config)
        # 顺序即生效顺序：首条隐藏路径拒绝，末条兜底拒绝
        kinds = [(p.mode, p.operations) for p in permissions]
        assert kinds[0][0] == "deny"  # /**/.* 与 /**/.*/**
        assert kinds[-1][0] == "deny" and "write" in kinds[-1][1]

    def test_backend_routes(self, db_agent_config, tmp_path):
        backend = build_backend(tmp_path, db_agent_config)
        assert backend.artifacts_root == "/artifacts"
        # /skills/ 与 /artifacts/ 均可读（P09 细节在 test_backend_permissions）


# ----------------------------------------------------------------------------
# doc 6.2 §7.5.2/§7.6：单层重试的模型构造核验与装配
# ----------------------------------------------------------------------------


class TestRetryModelConstruction:
    def test_enabled_builds_zero_retry_chatopenai(self, db_agent_config):
        """标准 YAML 开启：主模型客户端 max_retries 强制 0（同步/异步均核验）。"""
        from src.modules.db_deepagent.agent import build_model

        assert db_agent_config.model.retry.enabled is True
        model = build_model(db_agent_config)
        from langchain_openai import ChatOpenAI

        assert isinstance(model, ChatOpenAI)
        assert model.max_retries == 0
        assert model.root_client.max_retries == 0
        assert model.root_async_client.max_retries == 0

    def test_conflicting_override_rejected(self, db_agent_config):
        """启用项目重试时 params 显式 max_retries≠0 → 拒绝。"""
        from src.modules.db_deepagent.agent import build_model
        from src.modules.db_deepagent.errors import DbAgentConfigError

        cfg = db_agent_config.model_copy(deep=True)
        cfg.model.params = {**cfg.model.params, "max_retries": 2}
        with pytest.raises(DbAgentConfigError, match="max_retries"):
            build_model(cfg)

    def test_missing_key_has_neutral_role_and_registry_path(
        self, db_agent_config, monkeypatch
    ):
        from src.modules.db_deepagent.agent import build_model

        config = {
            "llm_providers": {
                "dashscope_openai": {
                    "adapter": "openai_chat_completions",
                    "api_key": None,
                    "base_url": "https://example.invalid/v1",
                    "models": {"qwen3.8-max": {}},
                }
            }
        }
        monkeypatch.setattr("src.services.llm_factory.get_config", lambda: config)

        with pytest.raises(ValueError) as exc_info:
            build_model(db_agent_config)

        assert str(exc_info.value).startswith(
            "model: llm_providers.dashscope_openai.api_key"
        )
        assert "model.params" not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("direct", "inherited", "rejected"),
        [
            (True, False, True),
            (None, True, True),
            (False, True, False),
            (None, None, False),
        ],
    )
    def test_stream_guard_before_client_construction(
        self, db_agent_config, monkeypatch, direct, inherited, rejected
    ):
        from unittest.mock import MagicMock

        from src.modules.db_deepagent.agent import build_model
        from src.modules.db_deepagent.errors import DbAgentConfigError
        from src.services.llm_factory import LLMWithMeta

        model_params = {} if inherited is None else {"stream": inherited}
        monkeypatch.setattr(
            "src.modules.db_deepagent.agent.resolve_llm_config",
            lambda *_args: (
                {"adapter": "openai_chat_completions"},
                {"params": model_params},
            ),
        )
        factory = MagicMock(
            return_value=LLMWithMeta(MagicMock(), "dashscope_openai", "qwen3.8-max")
        )
        monkeypatch.setattr("src.services.llm_factory.get_llm", factory)
        cfg = db_agent_config.model_copy(deep=True)
        cfg.model.retry.enabled = False
        cfg.model.params = {} if direct is None else {"stream": direct}

        if rejected:
            with pytest.raises(DbAgentConfigError, match="model.params.stream"):
                build_model(cfg)
            factory.assert_not_called()
        else:
            build_model(cfg)
            factory.assert_called_once()

    def test_inherited_non_bool_stream_has_registry_path(
        self, db_agent_config, monkeypatch
    ):
        from unittest.mock import MagicMock

        from src.modules.db_deepagent.agent import build_model
        from src.modules.db_deepagent.errors import DbAgentConfigError

        monkeypatch.setattr(
            "src.modules.db_deepagent.agent.resolve_llm_config",
            lambda *_args: (
                {"adapter": "openai_chat_completions"},
                {"params": {"stream": "false"}},
            ),
        )
        factory = MagicMock()
        monkeypatch.setattr("src.services.llm_factory.get_llm", factory)
        cfg = db_agent_config.model_copy(deep=True)
        cfg.model.retry.enabled = False
        cfg.model.params = {}

        with pytest.raises(DbAgentConfigError) as exc_info:
            build_model(cfg)

        assert "llm_providers.dashscope_openai.models.qwen3.8-max.params.stream" in str(
            exc_info.value
        )
        factory.assert_not_called()

    def test_native_dashscope_enabled_rejected(self, db_agent_config, monkeypatch):
        """原生 dashscope + enabled=true：启动校验拒绝并指引关闭。"""
        from src.modules.db_deepagent.agent import build_model
        from src.modules.db_deepagent.errors import DbAgentConfigError

        monkeypatch.setattr(
            "src.modules.db_deepagent.agent.resolve_llm_config",
            lambda *_args: ({"adapter": "dashscope_native"}, {}),
        )
        cfg = db_agent_config.model_copy(deep=True)
        cfg.model.provider = "dashscope"
        cfg.model.llm_name = "qwen-plus"
        cfg.model.retry.enabled = True
        with pytest.raises(DbAgentConfigError, match="model.retry.enabled=false"):
            build_model(cfg)

    def test_disabled_keeps_client_semantics(self, db_agent_config):
        """关闭开关：不注入 max_retries，主模型保持既有配置语义。"""
        from src.modules.db_deepagent.agent import build_model

        cfg = db_agent_config.model_copy(deep=True)
        cfg.model.retry.enabled = False
        assert "max_retries" not in cfg.model.params
        model = build_model(cfg)  # 不抛错（不进入核验分支）
        assert model.max_retries is None  # ChatOpenAI 默认（客户端内部默认 2）

    def test_verify_helper_rejects_none_client(self):
        """任一公开客户端为 None → 无法确认 → 拒绝（doc 6.2 §7.5.2）。"""
        from types import SimpleNamespace

        from src.modules.db_deepagent.agent import verify_single_layer_retry_clients
        from src.modules.db_deepagent.errors import DbAgentConfigError

        ok_client = SimpleNamespace(max_retries=0)
        verify_single_layer_retry_clients(
            SimpleNamespace(
                max_retries=0, root_client=ok_client, root_async_client=ok_client
            )
        )  # 不抛
        with pytest.raises(DbAgentConfigError):
            verify_single_layer_retry_clients(
                SimpleNamespace(
                    max_retries=0, root_client=None, root_async_client=ok_client
                )
            )
        with pytest.raises(DbAgentConfigError):
            verify_single_layer_retry_clients(
                SimpleNamespace(
                    max_retries=2, root_client=ok_client, root_async_client=ok_client
                )
            )

    def test_disabled_assembly_has_no_retry_middleware(self, assembly_services):
        """关闭时不装配项目层包装器（装配仍通过、工具集合不变）。"""
        from src.modules.db_deepagent.agent import build_db_agent, verify_assembly

        cfg = assembly_services.config.model_copy(deep=True)
        cfg.model.retry.enabled = False
        services = DbAgentServices(**{**assembly_services.__dict__, "config": cfg})
        handle = build_db_agent(services, session_id="t-noretry-asm")
        assert verify_assembly(handle.agent, cfg) == verify_assembly(
            handle.agent, assembly_services.config
        )
