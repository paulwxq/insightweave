"""doc 19 §10.3 第 12a 项：零任务直接回答的历史回读回归。

真实 DbAgentRunner（FakeAgent 零任务状态）→ 真实 Web 收尾钩子写历史 →
GET /api/v2/sessions/{sid}/turns 回读，验证 completed 与 needs_clarification
两种直接回答的正文、open_questions 与实时交付一致（doc 19 §11.2 场景 5）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 注册 db_deepagent 离线装配 fixture（依赖 tmp_path/monkeypatch 均为内置）
from src.tests.db_deepagent.conftest import (  # noqa: F401
    FakeAgent,
    assembly_services,
    db_agent_config,
)

pytestmark = pytest.mark.unit

LOCAL = {"Host": "127.0.0.1:8008"}
SOURCE_ID = "db_src_1"


def _direct_state(status: str, answer: str, open_questions: list[str]) -> dict:
    return {
        "messages": [],
        "db_executions": [],
        "db_analyses": [],
        "db_validations": {},
        "db_final_answers": {
            "run-x": {
                "run_id": "run-x",
                "answer": {
                    "status": status,
                    "answer": answer,
                    "findings": [],
                    "metric_definitions": [],
                    "limitations": [],
                    "open_questions": list(open_questions),
                    "unresolved_task_refs": [],
                },
            }
        },
    }


@pytest.mark.parametrize(
    "status,answer,questions",
    [
        ("completed", "大前天是 2026年11月12日。", []),
        (
            "needs_clarification",
            "请确认需要查询哪个月份。",
            ["需要查询哪个月份？"],
        ),
    ],
    ids=["completed", "clarify"],
)
def test_direct_answer_history_roundtrip(
    web_env,
    web_session,
    client,
    assembly_services,  # noqa: F811
    status,
    answer,
    questions,
):
    from src.api.v2.runs import _make_finalize_hook, execute_web_query
    from src.modules.db_deepagent.agent import DbAgentHandle
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.runner import DbAgentRunner

    def _runner(**kwargs):
        state = _direct_state(status, answer, questions)
        handle = DbAgentHandle(
            agent=FakeAgent(state),
            backend=None,
            permissions=[],
            harness_key="doc19-history",
            session_dir=Path("."),
        )
        hook = _make_finalize_hook(
            kwargs["webstore"], kwargs["readiness"], SOURCE_ID, kwargs["holder"]
        )
        real = DbAgentRunner(
            services=assembly_services,
            handle=handle,
            archive=QaArchive(web_env.store),
            finalize_hook=hook,
        )
        return real.run(
            kwargs["question"],
            kwargs["session_id"],
            request_id=kwargs["request_id"],
        )

    web_env.runtime._query_runner = _runner
    result = execute_web_query(
        web_env.runtime,
        session_id=web_session["session_id"],
        question="请问大前天是哪一天",
        source_id=SOURCE_ID,
    )
    assert result.status == status
    assert result.history_saved is True

    sid = web_session["session_id"]
    turns = client.get(f"/api/v2/sessions/{sid}/turns", headers=LOCAL)
    assert turns.status_code == 200
    items = turns.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["answer"] == answer
    assert items[0]["open_questions"] == questions
