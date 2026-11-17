"""PG checkpoint 跨版本验收探针 v2（doc 1 §6.5；主协调者第 1 轮 I1-01）。

相对 v1 的差异：不再用裸 ``StateGraph(DeepAgentState)``，改为**版本对应装配图**
（``create_deep_agent`` + 完整项目状态 ``DbDeepAgentState`` 全部账本通道 +
FilesystemMiddleware/SummarizationMiddleware/PatchToolCallsMiddleware 真实中间件栈），
并带可复核断言（消息序列、账本字段深度核对、真实摘要事件、跨版本回读/续跑）。

与生产 build_db_agent 的差异（有意裁剪，双方环境一致）：
- 模型为受控假模型（无真实 LLM 调用）；摘要阈值降至 60/30 以实际触发摘要；
- 不含 FinalAnswerGate/RunBudget/RequestAudit 中间件与业务工具（它们是项目自有
  代码、两环境同源，跨版本行为由 1174 项回归覆盖；探针聚焦 SDK 版本敏感面：
  状态通道/reducer、DeltaChannel、摘要事件、PostgresSaver 序列化）；
- 其余（真实 system_prompt、/skills/、backend、permissions、tool_command 账本
  写入协议）与生产同源。

旧 SDK 装配适配：deepagents 0.7.25 的 FilesystemMiddleware 只接受私有参数
``_permissions``；本探针按安装版本分支，不在旧 SDK 上硬跑当前 permissions 实现，
不修改正式代码或依赖。

数据边界：只写探针线程 ``dbda:upgrade-verify-v2:1``（含防御性清理与最终
cleanup 模式）；``read-real-old-thread`` 仅对既有真实业务线程只读 get_state，
不 invoke、不写任何行。

用法（在各环境解释器下运行，模式与环境版本强校验）:
  pg_checkpoint_probe_v2.py old-write            # 旧环境生成受控样本（R1/R2）
  pg_checkpoint_probe_v2.py new-cold-read-append # 新环境冷读→断言→追加 R3→再冷读
  pg_checkpoint_probe_v2.py old-readback         # 旧环境回退读→断言→续跑 R4
  pg_checkpoint_probe_v2.py read-real-old-thread # 新环境只读真实旧业务线程
  pg_checkpoint_probe_v2.py cleanup              # 删除探针线程三表行
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

sys.path.insert(0, "/opt/projects/insightweave/nl2sql")

MODE = sys.argv[1]
SESSION_ID = "upgrade-verify-v2"
THREAD_ID = f"dbda:{SESSION_ID}:1"
REAL_OLD_THREAD = "dbda:s_93138a3c178a4e20a453510431b69061:1"  # 升级前真实业务线程（只读）
REQUEST_ID = "req-upgrade-v2"

LEDGER_CHANNELS = (
    "db_validations",
    "db_executions",
    "db_analyses",
    "db_time_evidence",
    "db_dim_evidence",
    "db_tool_errors",
    "db_run_status",
    "db_final_answers",
)

EXECUTION_REQUIRED_KEYS = (
    "evidence_id",
    "tool_call_id",
    "run_id",
    "request_id",
    "purpose",
    "datasource_id",
    "sql_template",
    "executed_sql",
    "executed_sql_sha256",
    "result_ref",
    "columns",
    "saved_row_count",
    "source_truncated",
    "elapsed_ms",
    "warnings",
)
VALIDATION_REQUIRED_KEYS = (
    "key",
    "status",
    "normalized_sql",
    "expanded_sql_sha256",
    "placeholder_refs",
    "purpose",
    "thread_id",
    "request_id",
    "run_id",
    "datasource_id",
    "validation_fingerprint",
    "layer",
    "guard_evidence",
    "warnings",
    "validated_at",
    "tool_call_id",
)

LONG_REPLY = ("受控假模型长回复，用于累积近似 token 数触发真实摘要阈值，重复填充内容。" * 8)


def sdk_version() -> str:
    import importlib.metadata as im

    return im.version("deepagents")


def require_version(expected: str) -> None:
    actual = sdk_version()
    assert actual == expected, f"模式 {MODE!r} 需要 deepagents=={expected}，当前 {actual}"


# ---------------------------------------------------------------------------
# 受控假模型（与 src/tests/db_deepagent/conftest.py ScriptedChatModel 同构）
# ---------------------------------------------------------------------------


def _make_fake_model_class():
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import Field

    class ScriptedModel(BaseChatModel):
        model_name: str = "scripted-probe"
        responses: list = Field(default_factory=list)

        @property
        def _llm_type(self) -> str:
            return "scripted-probe"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            index = min(getattr(self, "_cursor", 0), len(self.responses) - 1)
            response = self.responses[index]
            object.__setattr__(self, "_cursor", getattr(self, "_cursor", 0) + 1)
            if isinstance(response, str):
                response = AIMessage(content=response)
            return ChatResult(generations=[ChatGeneration(message=response)])

    return ScriptedModel


# ---------------------------------------------------------------------------
# 装配（镜像 build_db_agent 的 SDK 相关子集；permissions 参数按版本分支）
# ---------------------------------------------------------------------------


def build_probe_assembly(saver, fake, ledger_tool):
    from deepagents import FilesystemMiddleware, create_deep_agent
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.summarization import SummarizationMiddleware
    from packaging.version import Version

    from src.modules.db_deepagent.agent import (
        build_backend,
        build_permissions,
        build_session_dir,
        build_summary_prompt,
        load_prompts,
    )
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.context import DbAgentContext
    from src.modules.db_deepagent.state import DbDeepAgentState

    cfg = load_db_agent_config().model_copy(deep=True)
    # 低阈值确保受控样本内实际触发真实摘要（与单测同口径；仅探针内改内存副本）
    cfg.summarization.summary_trigger_tokens = 60
    cfg.summarization.recent_context_tokens = 30

    session_dir = build_session_dir(cfg, SESSION_ID)
    backend = build_backend(session_dir, cfg)
    permissions = build_permissions(cfg)

    if Version(sdk_version()) < Version("0.7.31"):
        # 旧 SDK（0.7.25）：仅私有参数（当前正式代码的 permissions= 会 TypeError）
        perm_kwargs = {"_permissions": permissions}
    else:
        perm_kwargs = {"permissions": permissions}
    file_mw = FilesystemMiddleware(
        backend=backend,
        tools=["ls", "read_file", "glob", "grep"],
        tool_token_limit_before_evict=cfg.filesystem.tool_token_limit_before_evict,
        **perm_kwargs,
    )

    prompts = load_prompts()
    summary_model = fake.model_copy()
    summary_model.metadata = {
        **(fake.metadata or {}),
        "dbda_call_kind": "summarization",
    }
    summary_mw = SummarizationMiddleware(
        summary_model,
        backend=backend,
        summary_prompt=build_summary_prompt(prompts),
        trigger=("tokens", cfg.summarization.summary_trigger_tokens),
        keep=("tokens", cfg.summarization.recent_context_tokens),
        trim_tokens_to_summarize=cfg.summarization.trim_tokens_to_summarize,
    )

    agent = create_deep_agent(
        model=fake,
        system_prompt=prompts["agent"]["system_prompt"],
        tools=[ledger_tool],
        middleware=[file_mw, summary_mw, PatchToolCallsMiddleware()],
        subagents=[],
        skills=["/skills/"],
        permissions=permissions,
        backend=backend,
        state_schema=DbDeepAgentState,
        context_schema=DbAgentContext,
        checkpointer=saver,
        name="db_deepagent",
    )

    from langgraph.channels import DeltaChannel

    channels = agent.channels
    assert isinstance(channels.get("messages"), DeltaChannel), "messages 非 DeltaChannel"
    for name in LEDGER_CHANNELS:
        assert name in channels, f"装配图缺少账本通道 {name}"
    assert "_summarization_event" in channels, "装配图缺少 _summarization_event 通道"
    return agent


# ---------------------------------------------------------------------------
# 账本工具（真实 tool_command 协议写入账本通道）
# ---------------------------------------------------------------------------


def make_ledger_tool(round_tag: str):
    from langchain.tools import ToolRuntime
    from langchain_core.tools import tool
    from langgraph.types import Command

    from src.modules.db_deepagent.context import DbAgentContext
    from src.modules.db_deepagent.state import DbDeepAgentState
    from src.modules.db_deepagent.tools._envelope import tool_command

    # 旧 langchain-core 经 get_type_hints 从模块命名空间解析字符串注解
    # （本文件启用 from __future__ import annotations），先绑定再装饰
    globals()["ToolRuntime"] = ToolRuntime
    globals()["DbAgentContext"] = DbAgentContext
    globals()["DbDeepAgentState"] = DbDeepAgentState
    globals()["Command"] = Command

    run_id = f"run-probe-{round_tag}"

    @tool
    def record_probe_evidence(
        purpose: str,
        runtime: ToolRuntime[DbAgentContext, DbDeepAgentState],
    ) -> Command:
        """登记一条受控探针账本记录（执行/校验/状态/答案/证据）。"""
        execution = {
            "evidence_id": f"E-probe{round_tag}",
            "tool_call_id": runtime.tool_call_id or "",
            "run_id": run_id,
            "request_id": REQUEST_ID,
            "purpose": purpose,
            "datasource_id": "db_src_1",
            "sql_template": "select 1 as probe",
            "executed_sql": "select 1 as probe",
            "executed_sql_sha256": "0" * 64,
            "result_ref": f"res-probe-{round_tag}",
            "columns": [{"name": "probe", "pg_type": "integer"}],
            "saved_row_count": 1,
            "source_truncated": False,
            "elapsed_ms": 1,
            "warnings": [],
        }
        validation = {
            "key": f"probe-key-{round_tag}",
            "status": "valid",
            "normalized_sql": "select 1 as probe",
            "expanded_sql_sha256": "0" * 64,
            "placeholder_refs": [],
            "purpose": purpose,
            "thread_id": THREAD_ID,
            "request_id": REQUEST_ID,
            "run_id": run_id,
            "datasource_id": "db_src_1",
            "validation_fingerprint": "fp-probe",
            "layer": "all_passed",
            "guard_evidence": {"time": [], "dim": []},
            "warnings": [],
            "validated_at": "2026-11-09T00:00:00+08:00",
            "tool_call_id": runtime.tool_call_id or "",
        }
        status = {
            "run_id": run_id,
            "stop_reason": None,
            "counters": {"tool_call": 1},
            "final_answer_reminders": 0,
        }
        updates: dict = {
            "db_executions": [execution],
            "db_validations": {f"probe-key-{round_tag}": validation},
            "db_run_status": {run_id: status},
        }
        if round_tag == "r3":  # 新环境追加轮：覆盖 merge/追加两类 reducer 的新键
            updates["db_dim_evidence"] = [
                {
                    "dim_evidence_id": f"D-probe{round_tag}",
                    "run_id": run_id,
                    "source": "milvus_dim_hit",
                    "table": "public.orders",
                    "column": "status",
                    "values": ["已支付"],
                    "patterns": [],
                    "query_text": "已支付",
                    "ref": None,
                    "datasource_id": "db_src_1",
                    "inherited_from": None,
                }
            ]
            updates["db_final_answers"] = {
                run_id: {
                    "run_id": run_id,
                    "request_id": REQUEST_ID,
                    "tool_call_id": runtime.tool_call_id or "",
                    "answer": {"status": "completed", "answer": "probe ok"},
                    "submitted_at": "2026-11-09T00:00:00+08:00",
                }
            }
        if round_tag == "r1":  # 旧环境首轮：时间证据（operator.add 通道）
            updates["db_time_evidence"] = [
                {
                    "time_evidence_id": f"T-probe{round_tag}",
                    "run_id": run_id,
                    "source_text": "上月",
                    "grain": "month",
                    "start": "2026-10-01",
                    "end": "2026-11-01",
                    "timezone": "Asia/Shanghai",
                    "datasource_id": "db_src_1",
                    "inherited_from": None,
                }
            ]
        if round_tag == "r4":  # 旧环境回退续跑轮：工具错误通道（operator.add）
            updates["db_tool_errors"] = [
                {
                    "tool_call_id": runtime.tool_call_id or "",
                    "run_id": run_id,
                    "tool": "record_probe_evidence",
                    "error_type": "ProbeMarker",
                    "message": "受控标记",
                    "retryable": False,
                }
            ]
        return tool_command(
            json.dumps({"ok": True, "round": round_tag}, ensure_ascii=False),
            runtime.tool_call_id or "",
            updates,
        )

    return record_probe_evidence


# ---------------------------------------------------------------------------
# 连接（生产同源：平台池 + PostgresSaver(pool)）
# ---------------------------------------------------------------------------


def open_saver():
    from langgraph.checkpoint.postgres import PostgresSaver

    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.persistence.platform_pool import build_platform_pool

    cfg = load_db_agent_config()
    pool = build_platform_pool(cfg.platform_db)
    pool.open(wait=True)
    return pool, PostgresSaver(pool)


def make_context(run_tag: str):
    from src.modules.db_deepagent.context import DbAgentContext

    return DbAgentContext(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        request_id=REQUEST_ID,
        run_id=f"run-probe-{run_tag}",
        datasource_id="db_src_1",
        validation_fingerprint="fp-probe",
        now=datetime.now(UTC),
        timezone="Asia/Shanghai",
        user_texts=("受控探针问题",),
        carried_evidence=(),
        deadline_monotonic=float("inf"),
    )


def thread_row_counts(pool) -> dict:
    counts = {}
    with pool.connection() as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            row = conn.execute(
                f"select count(*) as n from db_deepagent.{table} "
                "where thread_id = %s",
                (THREAD_ID,),
            ).fetchone()
            counts[table] = row["n"]
    return counts


def delete_probe_rows(pool) -> dict:
    counts = {}
    with pool.connection() as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            cur = conn.execute(
                f"delete from db_deepagent.{table} where thread_id = %s",
                (THREAD_ID,),
            )
            counts[table] = cur.rowcount
        conn.commit()
    return counts


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def message_outline(values) -> list:
    return [type(m).__name__ for m in (values.get("messages") or [])]


def assert_message_texts(values, expected_texts: list[str]) -> None:
    msgs = values.get("messages") or []
    assert len(msgs) >= len(expected_texts), (
        f"消息数 {len(msgs)} 少于预期锚点数 {len(expected_texts)}"
    )
    joined = [str(getattr(m, "content", "")) for m in msgs]
    for anchor in expected_texts:
        hits = [anchor in text for text in joined]
        assert any(hits), f"未找到消息锚点 {anchor!r}；实际序列长度 {len(msgs)}"


def assert_ledger(values, *, execution_ids, validation_keys, status_runs,
                  time_ids=(), dim_ids=(), answer_runs=(), error_tools=()):
    execs = values.get("db_executions") or []
    assert [r["evidence_id"] for r in execs] == list(execution_ids), (
        f"db_executions 顺序不符: {[r['evidence_id'] for r in execs]}"
    )
    for rec in execs:
        missing = [k for k in EXECUTION_REQUIRED_KEYS if k not in rec]
        assert not missing, f"执行记录缺字段 {missing}: {sorted(rec)}"
    validations = values.get("db_validations") or {}
    assert sorted(validations) == sorted(validation_keys), (
        f"db_validations 键不符: {sorted(validations)}"
    )
    for rec in validations.values():
        missing = [k for k in VALIDATION_REQUIRED_KEYS if k not in rec]
        assert not missing, f"校验记录缺字段 {missing}"
    status = values.get("db_run_status") or {}
    assert sorted(status) == sorted(status_runs), f"db_run_status 键不符: {sorted(status)}"
    if time_ids:
        assert [r["time_evidence_id"] for r in values.get("db_time_evidence") or []] == list(time_ids)
    if dim_ids:
        assert [r["dim_evidence_id"] for r in values.get("db_dim_evidence") or []] == list(dim_ids)
    if answer_runs:
        assert sorted(values.get("db_final_answers") or {}) == sorted(answer_runs)
    if error_tools:
        assert [r["error_type"] for r in values.get("db_tool_errors") or []] == list(error_tools)


def assert_summary_event_readable(values) -> str:
    from src.modules.db_deepagent.agent import read_summary_for_handoff

    text = read_summary_for_handoff(values)
    assert text, "read_summary_for_handoff 未取到摘要文本"
    return text


# ---------------------------------------------------------------------------
# 模式实现
# ---------------------------------------------------------------------------


def _fresh_assembly(saver, ledger_tool_tag: str, *, tool_round: bool = True):
    """装配 + 受控假模型。tool_round=False 时模型只回长文本，不写账本（R2）。"""
    fake_cls = _make_fake_model_class()
    from langchain_core.messages import AIMessage

    if tool_round:
        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "record_probe_evidence",
                        "args": {"purpose": "query"},
                        "id": f"call_{ledger_tool_tag}",
                        "type": "tool_call",
                    }
                ],
            ),
            LONG_REPLY,
        ]
    else:
        responses = [LONG_REPLY]
    fake = fake_cls(responses=responses)
    tool = make_ledger_tool(ledger_tool_tag)
    return build_probe_assembly(saver, fake, tool), fake, config_for_thread()


def config_for_thread(thread_id: str = THREAD_ID):
    return {"configurable": {"thread_id": thread_id}}


def mode_old_write() -> None:
    require_version("0.7.25")
    pool, saver = open_saver()
    try:
        deleted = delete_probe_rows(pool)  # 防御：清掉历史半程残留
        if any(deleted.values()):
            print(f"[defensive-cleanup] {deleted}")
        agent, _fake, config = _fresh_assembly(saver, "r1")
        from langchain_core.messages import HumanMessage

        agent.invoke(
            {"messages": [HumanMessage(content="受控探针问题 R1（旧环境生成）")]},
            config=config,
            context=make_context("r1"),
        )
        values = agent.get_state(config).values
        assert message_outline(values) == [
            "HumanMessage",
            "AIMessage",
            "ToolMessage",
            "AIMessage",
        ], f"R1 消息序列不符: {message_outline(values)}"
        assert_message_texts(values, ["受控探针问题 R1（旧环境生成）", LONG_REPLY[:10]])
        assert_ledger(
            values,
            execution_ids=["E-prober1"],
            validation_keys=["probe-key-r1"],
            status_runs=["run-probe-r1"],
            time_ids=["T-prober1"],
        )
        text = assert_summary_event_readable(values)
        event = values["_summarization_event"]
        assert "cutoff_message_id" not in event, "旧 SDK 事件不应含新字段 cutoff_message_id"
        # R2：追加重叠轮（不写账本，仅消息），驱动跨轮摘要与更长线程
        agent2, _f2, cfg2 = _fresh_assembly(saver, "r2-plain", tool_round=False)
        from langchain_core.messages import HumanMessage as HM

        agent2.invoke(
            {"messages": [HM("受控探针问题 R2（旧环境追加重叠轮）")]},
            config=cfg2,
            context=make_context("r2"),
        )
        values2 = agent2.get_state(cfg2).values
        assert len(values2["messages"]) == 6, f"R2 后消息数应为 6: {len(values2['messages'])}"
        assert_summary_event_readable(values2)
        assert_ledger(
            values2,
            execution_ids=["E-prober1"],
            validation_keys=["probe-key-r1"],
            status_runs=["run-probe-r1"],
            time_ids=["T-prober1"],
        )
        print(f"[old-write] OK summary_len={len(text)} rows={thread_row_counts(pool)}")
        print("[old-write] 消息=6 账本=1执行/1校验/1状态/1时间证据 事件=旧结构可读")
    finally:
        pool.close()


def mode_new_cold_read_append() -> None:
    require_version("0.7.34")
    pool, saver = open_saver()
    try:
        # 冷读（新装配图 + 新进程 + 独立连接池）：完整断言旧环境样本
        agent, _fake, config = _fresh_assembly(saver, "r3")
        values = agent.get_state(config).values
        assert len(values["messages"]) == 6, f"冷读消息数应为 6: {len(values['messages'])}"
        assert_message_texts(
            values,
            ["受控探针问题 R1（旧环境生成）", "受控探针问题 R2（旧环境追加重叠轮）", LONG_REPLY[:10]],
        )
        assert_ledger(
            values,
            execution_ids=["E-prober1"],
            validation_keys=["probe-key-r1"],
            status_runs=["run-probe-r1"],
            time_ids=["T-prober1"],
        )
        cold_text = assert_summary_event_readable(values)
        # R3：新环境在旧线程上继续运行（受控追加：新执行/新校验/维度证据/最终答案）
        from langchain_core.messages import HumanMessage

        agent.invoke(
            {"messages": [HumanMessage(content="受控探针问题 R3（新环境追加）")]},
            config=config,
            context=make_context("r3"),
        )
        warm = agent.get_state(config).values
        assert len(warm["messages"]) == 10, f"R3 后消息数应为 10: {len(warm['messages'])}"
        assert_message_texts(warm, ["受控探针问题 R3（新环境追加）"])
        assert_ledger(
            warm,
            execution_ids=["E-prober1", "E-prober3"],
            validation_keys=["probe-key-r1", "probe-key-r3"],
            status_runs=["run-probe-r1", "run-probe-r3"],
            time_ids=["T-prober1"],
            dim_ids=["D-prober3"],
            answer_runs=["run-probe-r3"],
        )
        # 再冷读：全新连接 + 全新装配进程内实例
        agent_cold, _fc, cfg_cold = _fresh_assembly(saver, "r3-cold")
        cold2 = agent_cold.get_state(cfg_cold).values
        assert len(cold2["messages"]) == 10, f"再冷读消息数应为 10: {len(cold2['messages'])}"
        assert_ledger(
            cold2,
            execution_ids=["E-prober1", "E-prober3"],
            validation_keys=["probe-key-r1", "probe-key-r3"],
            status_runs=["run-probe-r1", "run-probe-r3"],
            time_ids=["T-prober1"],
            dim_ids=["D-prober3"],
            answer_runs=["run-probe-r3"],
        )
        event = cold2["_summarization_event"]
        assert isinstance(event.get("cutoff_message_id"), (str, type(None))), (
            "新 SDK 事件应含 cutoff_message_id 字段（str|None）"
        )
        final_text = assert_summary_event_readable(cold2)
        assert final_text == assert_summary_event_readable(warm), "冷/热摘要文本不一致"
        print(f"[new-cold-read-append] OK cold_text_len={len(cold_text)} rows={thread_row_counts(pool)}")
        print("[new-cold-read-append] 旧样本冷读全过→R3 追加→再冷读全过（2 执行/2 校验/2 状态/1 时间/1 维度/1 答案）")
    finally:
        pool.close()


def mode_old_readback() -> None:
    require_version("0.7.25")
    pool, saver = open_saver()
    try:
        agent, _fake, config = _fresh_assembly(saver, "r4")
        values = agent.get_state(config).values
        assert len(values["messages"]) == 10, f"回退冷读消息数应为 10: {len(values['messages'])}"
        assert_message_texts(values, ["受控探针问题 R3（新环境追加）"])
        assert_ledger(
            values,
            execution_ids=["E-prober1", "E-prober3"],
            validation_keys=["probe-key-r1", "probe-key-r3"],
            status_runs=["run-probe-r1", "run-probe-r3"],
            time_ids=["T-prober1"],
            dim_ids=["D-prober3"],
            answer_runs=["run-probe-r3"],
        )
        assert_summary_event_readable(values)  # 旧读取器读新事件（含新字段）不报错
        # R4：旧环境回退后继续运行（对新环境写入的线程追加，含反向 reducer 追加）
        from langchain_core.messages import HumanMessage

        agent.invoke(
            {"messages": [HumanMessage(content="受控探针问题 R4（旧环境回退续跑）")]},
            config=config,
            context=make_context("r4"),
        )
        final = agent.get_state(config).values
        assert len(final["messages"]) == 14, f"R4 后消息数应为 14: {len(final['messages'])}"
        assert_ledger(
            final,
            execution_ids=["E-prober1", "E-prober3", "E-prober4"],
            validation_keys=["probe-key-r1", "probe-key-r3", "probe-key-r4"],
            status_runs=["run-probe-r1", "run-probe-r3", "run-probe-r4"],
            time_ids=["T-prober1"],
            dim_ids=["D-prober3"],
            answer_runs=["run-probe-r3"],
            error_tools=["ProbeMarker"],
        )
        assert_summary_event_readable(final)
        print(f"[old-readback] OK rows={thread_row_counts(pool)}")
        print("[old-readback] 回退冷读全过→R4 续跑→账本 3 执行/3 校验/3 状态/1 工具错误，摘要可读")
    finally:
        pool.close()


def mode_read_real_old_thread() -> None:
    require_version("0.7.34")
    pool, saver = open_saver()
    try:
        agent, _fake, _config = _fresh_assembly(saver, "readonly")
        snap = agent.get_state(config_for_thread(REAL_OLD_THREAD))
        values = snap.values
        msgs = values.get("messages") or []
        assert msgs, "真实旧线程应含消息"
        from langchain_core.messages import BaseMessage

        for m in msgs:
            assert isinstance(m, BaseMessage), f"消息反序列化类型异常: {type(m)}"
        execs = values.get("db_executions") or []
        validations = values.get("db_validations") or {}
        answers = values.get("db_final_answers") or {}
        assert execs and validations and answers, (
            "真实旧线程应含业务账本（执行/校验/答案）"
        )
        for rec in execs:
            missing = [k for k in EXECUTION_REQUIRED_KEYS if k not in rec]
            assert not missing, f"真实执行记录缺字段 {missing}"
        for rec in validations.values():
            missing = [k for k in VALIDATION_REQUIRED_KEYS if k not in rec]
            assert not missing, f"真实校验记录缺字段 {missing}"
        for rec in answers.values():
            assert rec.get("run_id") and rec.get("answer"), "最终答案记录缺字段"
        from src.modules.db_deepagent.agent import read_summary_for_handoff

        summary = read_summary_for_handoff(values)  # 无事件→None；有事件→文本；不得抛错
        print(
            f"[read-real-old-thread] OK {REAL_OLD_THREAD} messages={len(msgs)} "
            f"validations={len(validations)} executions={len(execs)} "
            f"final_answers={len(answers)} summary={'有' if summary else '无事件'}"
        )
    finally:
        pool.close()


def mode_cleanup() -> None:
    pool, _saver = open_saver()
    try:
        deleted = delete_probe_rows(pool)
        left = thread_row_counts(pool)
        assert not any(left.values()), f"清理后仍有残留: {left}"
        print(f"[cleanup] {deleted}; 残留={left}")
    finally:
        pool.close()


def main() -> None:
    print(f"== pg_checkpoint_probe_v2 mode={MODE} deepagents={sdk_version()} ==")
    handlers = {
        "old-write": mode_old_write,
        "new-cold-read-append": mode_new_cold_read_append,
        "old-readback": mode_old_readback,
        "read-real-old-thread": mode_read_real_old_thread,
        "cleanup": mode_cleanup,
    }
    if MODE not in handlers:
        raise ValueError(MODE)
    handlers[MODE]()
    print(f"== mode={MODE} PASSED ==")


if __name__ == "__main__":
    main()
