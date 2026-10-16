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
    def test_step4_profile_has_13_tools(self, db_agent_config: DbAgentConfig):
        names = stage_expected_tool_names(db_agent_config, include_result_tools=False)
        assert len(names) == 13
        assert "read_result" not in names and "analyze_result" not in names
        assert names == set(STEP4_TOOLS)

    def test_step5_default_15_tools(self, db_agent_config: DbAgentConfig):
        """步骤 5 默认口径：11（业务+提交）+ 4（文件）= 15（§10.6）。"""
        names = stage_expected_tool_names(db_agent_config)
        assert len(names) == 15
        assert {"read_result", "analyze_result"} <= names

    def test_todo_list_adds_write_todos(self, db_agent_config: DbAgentConfig):
        cfg = db_agent_config.model_copy(deep=True)
        cfg.features.todo_list = True
        names = stage_expected_tool_names(cfg)
        assert "write_todos" in names and len(names) == 16

    def test_forbidden_tools_absent(self):
        for name in ("write_file", "edit_file", "delete", "execute", "task"):
            assert name not in STEP4_TOOLS


class TestAssembly:
    def test_assembly_passes_and_verifies(self, assembly_services):
        handle = build_db_agent(assembly_services, session_id="t-assembly")
        expected = verify_assembly(handle.agent, assembly_services.config)
        assert len(expected) == 15
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
