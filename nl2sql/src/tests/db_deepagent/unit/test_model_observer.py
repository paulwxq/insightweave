"""ModelCallObserver 单元测试（docs/v4/5 §11.2.1、§18.2 模型观测完整性）

归属取实例、用途取 metadata、尝试 ID 去重、用量不重复累计、未知不填零、
finalize 幂等。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.modules.db_deepagent.middleware.model_observer import ModelCallObserver

pytestmark = pytest.mark.unit


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


def _llm_result(inp: int | None, out: int | None):
    message = SimpleNamespace(
        usage_metadata=_usage(inp, out) if inp is not None else None
    )
    generation = SimpleNamespace(message=message)
    return SimpleNamespace(generations=[[generation]], llm_output=None)


class TestObserver:
    def test_dedupe_start_events(self, tmp_path):
        """on_chat_model_start 与 on_llm_start 同一尝试只计一次。"""
        observer = ModelCallObserver("req", "run", tmp_path / "audit.jsonl")
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_end(_llm_result(10, 5), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.input_tokens == 10 and snapshot.output_tokens == 5

    def test_summary_vs_agent_kind(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        a1, a2 = uuid4(), uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=a1, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_chat_model_start(
            {}, [], run_id=a2, metadata={"dbda_call_kind": "summarization"}
        )
        observer.on_llm_end(_llm_result(7, 3), run_id=a1)
        observer.on_llm_end(_llm_result(4, 2), run_id=a2)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 2
        assert snapshot.summary_model_calls == 1
        assert snapshot.input_tokens == 11 and snapshot.output_tokens == 5

    def test_unknown_usage_marks_incomplete(self, tmp_path):
        """任一调用用量未知 → 合计 None、完整性 False；不以零填补。"""
        observer = ModelCallObserver("req", "run", None)
        a1, a2 = uuid4(), uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=a1, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_chat_model_start(
            {}, [], run_id=a2, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_end(_llm_result(10, 4), run_id=a1)
        observer.on_llm_end(_llm_result(None, None), run_id=a2)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 2
        assert snapshot.input_tokens is None and snapshot.output_tokens is None
        assert snapshot.token_usage_complete is False

    def test_missing_kind_marks_anomaly(self, tmp_path):
        """用途标记缺失：不能默认归为主模型，统计标记不完整。"""
        observer = ModelCallObserver("req", "run", None)
        attempt = uuid4()
        observer.on_chat_model_start({}, [], run_id=attempt, metadata={})
        observer.on_llm_end(_llm_result(5, 5), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.summary_model_calls == 0
        assert snapshot.token_usage_complete is False
        assert snapshot.anomalies

    def test_error_attempt_counted_no_usage(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "agent"}
        )
        observer.on_llm_error(RuntimeError("boom"), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.total_model_calls == 1
        assert snapshot.input_tokens is None

    def test_finalize_idempotent_ignores_late_callbacks(self, tmp_path):
        observer = ModelCallObserver("req", "run", None)
        first = observer.finalize()
        late = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=late, metadata={"dbda_call_kind": "agent"}
        )
        assert observer.finalize().total_model_calls == first.total_model_calls

    def test_no_calls_zero_and_complete(self, tmp_path):
        snapshot = ModelCallObserver("req", "run", None).finalize()
        assert snapshot.total_model_calls == 0
        assert snapshot.input_tokens == 0 and snapshot.token_usage_complete is True

    def test_attribution_from_instance_not_metadata(self, tmp_path):
        """摘要显式 metadata 无业务归属：快照归属仍取构造实例（§11.2.1）。"""
        observer = ModelCallObserver("req-9", "run-9", None)
        attempt = uuid4()
        observer.on_chat_model_start(
            {}, [], run_id=attempt, metadata={"dbda_call_kind": "summarization"}
        )
        observer.on_llm_end(_llm_result(3, 1), run_id=attempt)
        snapshot = observer.finalize()
        assert snapshot.request_id == "req-9" and snapshot.run_id == "run-9"
        assert snapshot.summary_model_calls == 1
