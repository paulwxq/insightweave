"""LLM 版时间解析器测试（doc 16 §9.2：模拟 flash 响应，离线）"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver
from src.modules.db_deepagent.services.time_resolver import (
    TimeResolver,
    build_time_resolver,
)
from src.tests.db_deepagent.conftest import ScriptedTimeModel, fake_time_resolver

pytestmark = pytest.mark.unit


class _BoomModel:
    def invoke(self, messages, config=None):
        raise TimeoutError("flash 超时")


def _ok_json(start, end, grain="day", interpretation="测试区间"):
    return json.dumps(
        {
            "status": "ok",
            "start": start,
            "end": end,
            "grain": grain,
            "interpretation": interpretation,
            "clarification_question": None,
        },
        ensure_ascii=False,
    )


def _clarify_json(question="请说明具体时间范围"):
    return json.dumps(
        {
            "status": "needs_clarification",
            "start": None,
            "end": None,
            "grain": None,
            "interpretation": "无法确定",
            "clarification_question": question,
        },
        ensure_ascii=False,
    )


def _resolver(response: str) -> TimeResolver:
    return TimeResolver(ScriptedTimeModel(default_response=response), "TEST-SYSTEM")


@pytest.fixture()
def ctx(db_agent_context: DbAgentContext) -> DbAgentContext:
    return replace(
        db_agent_context,
        user_texts=(
            "前天的订单有多少",
            "截至今天中午12点的订单",
            "统计2026年9月的订单",
        ),
    )


class TestResolver:
    def test_ok_path_registers_evidence(self, ctx):
        r = _resolver(_ok_json("2026-11-13", "2026-11-14")).resolve(
            ctx, source_text="前天"
        )
        assert r.ok
        ev = r.evidence
        assert ev["start"] == "2026-11-13" and ev["end"] == "2026-11-14"
        assert ev["grain"] == "day"
        assert ev["timezone"] == "Asia/Shanghai"
        assert ev["source_text"] == "前天"
        assert ev["time_evidence_id"].startswith("T-")
        assert r.interpretation == "测试区间"

    def test_clarification_skips_bound_checks(self, ctx):
        """§9.2：澄清结果两端为 null 直接返回澄清信封，不误判非法输出。"""
        r = _resolver(_clarify_json("哪个前天？")).resolve(ctx, source_text="前天")
        assert not r.ok
        assert r.error_code == "ambiguous_time_reference"
        assert "哪个前天" in r.message
        assert r.evidence is None

    def test_invalid_json(self, ctx):
        r = _resolver("这不是 JSON").resolve(ctx, source_text="前天")
        assert not r.ok and r.error_code == "time_parse_invalid_output"

    def test_invalid_date(self, ctx):
        r = _resolver(_ok_json("2025-02-29", "2025-03-01")).resolve(
            ctx, source_text="前天"
        )
        assert not r.ok and r.error_code == "time_parse_invalid_output"

    def test_grain_illegal(self, ctx):
        r = _resolver(_ok_json("2026-11-13", "2026-11-14", grain="century")).resolve(
            ctx, source_text="前天"
        )
        assert not r.ok and r.error_code == "time_parse_invalid_output"

    def test_reversed_interval(self, ctx):
        r = _resolver(_ok_json("2026-11-14", "2026-11-13")).resolve(
            ctx, source_text="前天"
        )
        assert not r.ok and r.error_code == "time_parse_invalid_output"

    def test_both_bounds_null(self, ctx):
        r = _resolver(_ok_json(None, None)).resolve(ctx, source_text="前天")
        assert not r.ok and r.error_code == "time_parse_invalid_output"

    def test_model_error(self, ctx):
        r = TimeResolver(_BoomModel(), "S").resolve(ctx, source_text="前天")
        assert not r.ok and r.error_code == "time_parse_model_error"

    def test_source_not_in_question(self, ctx):
        r = _resolver(_ok_json("2026-11-13", "2026-11-14")).resolve(
            ctx, source_text="上个月"
        )
        assert not r.ok and r.error_code == "time_text_not_in_question"


class TestInputContents:
    def test_human_payload_beijing_time(self, ctx, monkeypatch):
        """输入含当前问题/reference_now（北京时间）/context；记录供审计。"""
        captured: dict[str, Any] = {}

        class _Capture(ScriptedTimeModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                captured["system"] = messages[0].content
                captured["human"] = messages[-1].content
                return super()._generate(messages, stop, run_manager, **kwargs)

        resolver = TimeResolver(
            _Capture(default_response=_ok_json("2026-11-13", "2026-11-14")),
            "TEST-SYSTEM",
        )
        r = resolver.resolve(ctx, source_text="前天", context="订单按天统计")
        assert r.ok
        payload = json.loads(captured["human"])
        assert payload["source_text"] == "前天"
        assert payload["current_question"] == "统计2026年9月的订单"
        assert payload["context"] == "订单按天统计"
        assert payload["timezone"] == "Asia/Shanghai"
        # 固定时钟 2026-10-13 12:00+08:00 → 北京时间原样
        assert payload["reference_now"].startswith("2026-10-13T12:00:00")
        assert captured["system"] == "TEST-SYSTEM"


class TestBuildTimeResolver:
    def test_overrides_timeout_metadata(self, monkeypatch, db_agent_config):
        """§9.2.4：timeout 映射 + dbda_call_kind 元数据 + 真实提示词加载。"""
        import src.services.llm_factory as factory

        recorded: dict[str, Any] = {}

        class _Meta:
            def __init__(self):
                self.llm = ScriptedTimeModel()
                self.llm.metadata = {}

        def fake_get_llm(profile, **overrides):
            recorded["profile"] = profile
            recorded["overrides"] = overrides
            meta = _Meta()
            meta.llm = ScriptedTimeModel()
            return type("LLMWithMeta", (), {"llm": meta.llm})()

        monkeypatch.setattr(factory, "get_llm", fake_get_llm)
        resolver = build_time_resolver(db_agent_config)
        assert recorded["profile"] == db_agent_config.time_resolution.llm_profile
        ov = recorded["overrides"]
        assert (
            ov.get("timeout") == db_agent_config.time_resolution.request_timeout_seconds
        )
        assert ov.get("max_retries") == 2
        assert ov.get("response_format") == {"type": "json_object"}
        assert resolver._model.metadata["dbda_call_kind"] == "time_resolution"
        assert "自然语言时间表达" in resolver._system_prompt  # 真实提示词已加载

    def test_missing_profile_rejected(self, db_agent_config):
        from src.modules.db_deepagent.config import (
            DbAgentConfigError,
            _check_time_resolution,
        )

        bad = db_agent_config.model_copy(
            update={
                "time_resolution": db_agent_config.time_resolution.model_copy(
                    update={"llm_profile": "no_such_profile"}
                )
            }
        )
        with pytest.raises(DbAgentConfigError, match="time_resolution"):
            _check_time_resolution(bad)


class TestObserverKind:
    def test_time_resolution_attempt_counted(self, db_agent_context):
        """§9.2.5：内部调用用途被 observer 识别并计入总数，无 kind anomaly。"""
        from langchain_core.messages import HumanMessage

        model = ScriptedTimeModel(default_response=_ok_json("2026-11-13", "2026-11-14"))
        model.metadata = {"dbda_call_kind": "time_resolution"}
        observer = ModelCallObserver("req-t", "run-t", None)
        model.invoke([HumanMessage(content="{}")], config={"callbacks": [observer]})
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.anomalies == []  # 用途被识别，不产生非法用途告警


# ============================================================================
# Codex R1 修复配套测试（C1-01/02/04/06 + §9.2.7 guard 联通）
# ============================================================================


class TestBuildServicesWiring:
    def test_build_services_constructs_time_resolver(
        self, db_agent_config, monkeypatch, tmp_path
    ):
        """C1-01/C2-01：真实 build_services 构造成功且注入 LLM 解析器。

        外部连接设施（业务库/Milvus/Neo4j/结果目录）全部替换为假对象，
        工作目录走 tmp_path——不打开任何真实连接池、不查询真实设施。
        """
        import src.modules.db_deepagent.services as svc
        import src.services.llm_factory as factory

        created: dict[str, Any] = {}

        class _FakeConn:
            def __init__(self, *a, **k):
                pass

            def open(self):
                return None

            def close(self):
                return None

        monkeypatch.setattr(
            factory,
            "get_llm",
            lambda profile, **ov: (
                created.update(profile=profile, overrides=ov),
                type(
                    "LLMWithMeta",
                    (),
                    {"llm": ScriptedTimeModel()},
                )(),
            )[1],
        )
        monkeypatch.setattr(svc, "BusinessDbService", _FakeConn)
        monkeypatch.setattr(svc, "DbEmbeddingClient", _FakeConn)
        monkeypatch.setattr(svc, "DbMilvusRetrieval", _FakeConn)
        monkeypatch.setattr(svc, "JoinGraphService", _FakeConn)
        monkeypatch.setattr(svc, "_neo4j_manager", lambda cfg: None)
        monkeypatch.setattr(svc, "ResultStore", _FakeConn)
        cfg = db_agent_config.model_copy(
            update={
                "workspace": db_agent_config.workspace.model_copy(
                    update={"root": str(tmp_path)}
                )
            }
        )
        services = svc.build_services(
            cfg,
            source_id="db_src_1",
            verify_embedding=False,
        )
        try:
            assert isinstance(services.time_resolver, TimeResolver)
            assert created["profile"] == cfg.time_resolution.llm_profile
            assert created["overrides"]["timeout"] == (
                cfg.time_resolution.request_timeout_seconds
            )
        finally:
            services.close()  # 容器统一释放路径（join_graph→milvus→business_db）


class TestStrictBoundFormats:
    """C1-02：格式白名单——偏移/紧凑/异常分隔/混合偏移全部 invalid_output。"""

    @pytest.mark.parametrize(
        "start,end",
        [
            (
                "2026-11-13T00:00:00+08:00",
                "2026-11-14T00:00:00",
            ),  # 混合偏移（原 TypeError 逃逸）
            ("2026-11-13T00:00:00+00:00", "2026-11-14T00:00:00+00:00"),  # 双端偏移
            (None, "2026-11-14T00:00:00Z"),  # Z 后缀
            ("20261113", "20261114"),  # 紧凑日期
            ("2026-11-13'00:00:00", "2026-11-14T00:00:00"),  # 异常分隔符
            ("2026-11-13T00:00:00.123456", "2026-11-14T00:00:00"),  # 小数秒
        ],
    )
    def test_illegal_formats_rejected(self, ctx, start, end):
        r = _resolver(_ok_json(start, end)).resolve(ctx, source_text="前天")
        assert not r.ok
        assert r.error_code == "time_parse_invalid_output"

    def test_legal_mixed_grain_passes(self, ctx):
        """合法：日期与日期时间混用（doc 允许的两种白名单格式）。"""
        r = _resolver(_ok_json("2026-11-13", "2026-11-14T00:00:00")).resolve(
            ctx, source_text="前天"
        )
        assert r.ok

    def test_tool_node_rejects_offset_without_ledger(self, db_agent_context):
        """经真实 ToolNode：非法格式返回错误信封且不写 db_time_evidence。"""
        from types import SimpleNamespace

        from langgraph.prebuilt import ToolNode

        from src.modules.db_deepagent.tools.guard_tools import (
            make_resolve_time_range_tool,
        )
        from src.tests.db_deepagent.conftest import invoke_tool_node

        ctx = replace(db_agent_context, user_texts=("前天的订单",))
        resolver = TimeResolver(
            ScriptedTimeModel(
                script={
                    "前天": _ok_json("2026-11-13T00:00:00+08:00", "2026-11-14T00:00:00")
                }
            ),
            "TEST-SYSTEM",
        )
        node = ToolNode(
            [make_resolve_time_range_tool(SimpleNamespace(time_resolver=resolver))]
        )
        updates, msg = invoke_tool_node(
            node,
            [("resolve_time_range", {"source_text": "前天"}, "tc-off")],
            ctx,
        )
        payload = json.loads(msg.content)
        assert payload["status"] == "error"
        assert payload["error"]["error_type"] == "time_parse_invalid_output"
        assert "db_time_evidence" not in updates

    def test_dashscope_provider_timeout_rejected(self, db_agent_config, monkeypatch):
        """C1-06：原生 dashscope provider 的 timeout 映射被工厂显式拒绝。"""
        from src.services import config_loader

        real_get_config = config_loader.get_config

        def patched_get_config():
            real = real_get_config()
            profiles = dict(real.get("llm_profiles") or {})
            profiles["dashscope_native"] = {
                "provider": "dashscope",
                "model": "qwen-plus",
            }
            providers = dict(real.get("llm_providers") or {})
            providers["dashscope"] = {"api_key": "test-key"}
            return {"llm_profiles": profiles, "llm_providers": providers}

        monkeypatch.setattr(
            "src.modules.db_deepagent.config.get_config", patched_get_config
        )
        monkeypatch.setattr("src.services.config_loader.get_config", patched_get_config)
        monkeypatch.setattr("src.services.llm_factory.get_config", patched_get_config)
        from src.modules.db_deepagent.services.time_resolver import build_time_resolver

        bad_cfg = db_agent_config.model_copy(
            update={
                "time_resolution": db_agent_config.time_resolution.model_copy(
                    update={
                        "llm_profile": "dashscope_native",
                        # 剥离 max_retries：原生 dashscope 同样不支持它，
                        # 留着会让断言命中重试报错而非 timeout 报错
                        "overrides": {"response_format": {"type": "json_object"}},
                    }
                )
            }
        )
        with pytest.raises(ValueError, match="timeout"):
            build_time_resolver(bad_cfg)


class TestDebugLogging:
    def test_debug_logs_system_human_response(self, ctx, capsys):
        """C1-04：DEBUG 开启时记录 System+Human（调用前）与响应（调用后）。"""
        import logging as _logging

        from src.modules.db_deepagent.services import time_resolver as mod

        marker_system = "MARKER-SYSTEM-42"
        expected_response = _ok_json("2026-11-13", "2026-11-14")
        resolver = TimeResolver(
            ScriptedTimeModel(default_response=expected_response),
            marker_system,
        )
        records: list[str] = []

        class _Cap(_logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Cap(level=_logging.DEBUG)
        old_level = mod.logger.level
        mod.logger.addHandler(handler)
        mod.logger.setLevel(_logging.DEBUG)
        try:
            r = resolver.resolve(ctx, source_text="前天")
            assert r.ok
            text = "\n".join(records)
            assert marker_system in text  # System 输入已记录（调用前）
            assert "source_text" in text  # Human 输入
            out_records = [m for m in records if "时间解析模型输出" in m]
            # C3-01：断言完整假响应原文出现在输出记录中（而非片段）
            assert out_records and expected_response in out_records[0]

        finally:
            mod.logger.removeHandler(handler)
            mod.logger.setLevel(old_level)

    def test_debug_disabled_no_payload(self, ctx):
        """关 DEBUG 不排版正文（isDebugEnabled 守卫）。"""
        import logging as _logging

        from src.modules.db_deepagent.services import time_resolver as mod

        resolver = TimeResolver(
            ScriptedTimeModel(default_response=_ok_json("2026-11-13", "2026-11-14")),
            "S",
        )
        records: list[str] = []

        class _Cap(_logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Cap(level=_logging.DEBUG)
        old_level = mod.logger.level
        mod.logger.addHandler(handler)
        mod.logger.setLevel(_logging.INFO)
        try:
            r = resolver.resolve(ctx, source_text="前天")
            assert r.ok
            # INFO 摘要行允许；DEBUG 正文（System/Human 输入、模型输出）不得出现
            assert not any(
                "时间解析输入" in m or "时间解析模型输出" in m for m in records
            )
        finally:
            mod.logger.removeHandler(handler)
            mod.logger.setLevel(old_level)

    def test_model_failure_still_logs_inputs(self, ctx):
        """模型调用失败时调用前输入日志已落（C1-04 补充）。"""
        import logging as _logging

        from src.modules.db_deepagent.services import time_resolver as mod

        marker = "MARKER-SYS-FAIL"
        resolver = TimeResolver(_BoomModel(), marker)
        records: list[str] = []

        class _Cap(_logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Cap(level=_logging.DEBUG)
        old_level = mod.logger.level
        mod.logger.addHandler(handler)
        mod.logger.setLevel(_logging.DEBUG)
        try:
            r = resolver.resolve(ctx, source_text="前天")
            assert not r.ok and r.error_code == "time_parse_model_error"
            assert any(marker in m for m in records)  # 输入已先于失败记录
        finally:
            mod.logger.removeHandler(handler)
            mod.logger.setLevel(old_level)


class TestGuardInterop:
    def test_tool_evidence_passes_guard(self, db_agent_context):
        """§9.2.7：新解析器产出证据 → 真实 guard 对日粒度 SQL 放行（联通回归）。"""
        from types import SimpleNamespace

        from src.modules.db_deepagent.config import GuardCheckConfig
        from src.modules.db_deepagent.services.dimension_probe import (
            DimensionProbeBuilder,
        )
        from src.modules.db_deepagent.services.sql_guard import SqlEvidenceGuard

        class _Db:
            def get_columns(self, schema, table):
                if (schema, table) == ("sales", "orders"):
                    return [
                        SimpleNamespace(
                            column_name="d", data_type="date", is_nullable=True
                        )
                    ]
                return []

        guard = SqlEvidenceGuard(
            GuardCheckConfig(
                enabled=True,
                forbid_realtime_clock_in_filters=True,
                require_time_evidence=True,
                require_dimension_evidence=True,
            ),
            _Db(),
            DimensionProbeBuilder(),
        )
        ctx = replace(db_agent_context, user_texts=("统计2026年9月的订单",))
        ev = fake_time_resolver().resolve(ctx, source_text="2026年9月").evidence
        assert ev is not None and ev["start"] == "2026-09-01"
        sql = (
            "SELECT COUNT(*) FROM sales.orders "
            "WHERE d >= '2026-09-01' AND d < '2026-10-01'"
        )
        g = guard.check(ctx, sql, time_evidence=[ev], dim_evidence=[])
        assert g.ok, g.errors
