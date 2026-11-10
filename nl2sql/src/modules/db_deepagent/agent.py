"""Agent 装配（docs/v4/5 §10）

步骤 1 只交付 _sdk_adapter 小节与模型构造；build_db_agent（backend、middleware、
工具、装配后验证）在步骤 4 落地。SDK 私有依赖集中在 _sdk_adapter，装配与 Runner
只调用这里的适配函数，不在其他位置导入私有接口或读取 SDK 私有状态键。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.context import DbAgentContext
from src.modules.db_deepagent.state import DbDeepAgentState
from src.modules.db_deepagent.tools import build_business_tools
from src.modules.db_deepagent.errors import (
    AssemblyVerificationError,
    HarnessProfileError,
)
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.agent")


# ============================================================================
# _sdk_adapter：SDK 私有依赖的唯一入口（§10.2；按 deepagents==0.7.25 核对）
# ============================================================================


def apply_request_timeout(model: BaseChatModel, timeout_seconds: float) -> None:
    """构造后设置单次模型 HTTP 请求超时（§11.1；仅 ChatTongyi 路径）。

    ChatTongyi：llm_factory 拒绝 timeout 覆盖（C12），DashScope SDK 经
    _build_api_request 在每次调用时接收 request_timeout（默认 300 秒），
    ChatTongyi 把 model_kwargs 原样并入 Generation.call 参数——构造后
    写入 model_kwargs 即可生效。

    ChatOpenAI（DashScope compatible-mode，qwen3.8-max 承载路径）：
    langchain-openai 1.7.x 在构造期 model_validator 中按当时的 request_timeout
    构建 root_client／httpx 客户端，构造后改字段不会重建客户端——超时必须在
    构造时经 llm_factory 的 timeout 覆盖参数传入（见 build_model）。
    """
    from langchain_community.chat_models import (
        ChatTongyi,
    )  # 与 llm_factory 的导入路径一致

    if isinstance(model, ChatTongyi):
        model.model_kwargs["request_timeout"] = timeout_seconds
    else:
        # 兜底：非 Tongyi 时仍写字段（客户端若尚未构建则生效）
        model.request_timeout = timeout_seconds


def effective_request_timeout(model: BaseChatModel) -> float | None:
    """读取当前生效的单次请求超时（§11.1 断言用；doctor 与单测共用）。

    ChatTongyi 查 _invocation_params（model_kwargs 已并入）；ChatOpenAI
    优先读已构建客户端的实际超时（root_client.timeout），未构建时退回字段。
    """
    from langchain_community.chat_models import ChatTongyi

    if isinstance(model, ChatTongyi):
        return model._invocation_params([], None).get("request_timeout")
    for attr in ("root_client", "root_async_client"):
        client = getattr(model, attr, None)
        timeout = getattr(client, "timeout", None)
        if timeout is not None:
            return timeout
    return getattr(model, "request_timeout", None)


def register_and_verify_harness_profile(
    model: BaseChatModel, cfg: DbAgentConfig
) -> str:
    """按实际模型标识注册并验证精确匹配键（§10.2）。

    只注册 provider:identifier 精确键，不注册 provider 级键，避免影响其他模型
    （概要设计 §8.2）。期望键在配置中声明时，与实际解析键不一致即初始化失败。
    """
    from deepagents import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
        register_harness_profile,
    )
    from deepagents._models import get_model_identifier, get_model_provider
    from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model

    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if not provider or not identifier:
        raise HarnessProfileError("无法确认 HarnessProfile 匹配键，需要适配模型标识")
    key = f"{provider}:{identifier}"
    expected = cfg.model.expected_harness_key
    if expected is not None and expected != key:
        raise HarnessProfileError(
            f"配置的匹配键 {expected!r} 与模型实际解析键 {key!r} 不一致"
        )
    register_harness_profile(
        key,
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
        ),
    )
    resolved = _harness_profile_for_model(model, None)
    gp = resolved.general_purpose_subagent
    if gp is None or gp.enabled is not False:
        raise HarnessProfileError(f"HarnessProfile 未命中: {key}")
    logger.info(
        "HarnessProfile 已命中: class=%s provider=%s identifier=%s key=%s",
        type(model).__name__,
        provider,
        identifier,
        key,
    )
    return key


def read_summary_for_handoff(state_values: dict) -> str | None:
    """从旧线程 get_state().values 读取最近一次摘要文本（§10.2，K21）。

    - 键缺失或值为 None：尚无摘要，返回 None（交接消息不额外生成摘要）
    - 结构不符合约定（非映射／summary_message 非 HumanMessage／cutoff_index 非法）：
      抛 AssemblyVerificationError，由 Runner 在 P0 记录为运行失败，不静默当"尚无摘要"
    - _summarization_session_id 只做存在性核对，不影响摘要文本读取；不跨线程复制
    """
    event = state_values.get("_summarization_event")
    if event is None:
        return None
    if not isinstance(event, dict):
        raise AssemblyVerificationError("_summarization_event 不是映射")
    summary_message = event.get("summary_message")
    if not isinstance(summary_message, HumanMessage):
        raise AssemblyVerificationError(
            "_summarization_event.summary_message 不是 HumanMessage"
        )
    cutoff_index = event.get("cutoff_index")
    if (
        not isinstance(cutoff_index, int)
        or isinstance(cutoff_index, bool)
        or cutoff_index < 0
    ):
        raise AssemblyVerificationError(f"cutoff_index 非法: {cutoff_index!r}")
    message_count = len(state_values.get("messages", []))
    if cutoff_index > message_count:
        raise AssemblyVerificationError(
            f"cutoff_index {cutoff_index} 超过消息数 {message_count}"
        )
    file_path = event.get("file_path")
    if file_path is not None and not isinstance(file_path, str):
        raise AssemblyVerificationError(f"file_path 非法: {file_path!r}")
    session_id = state_values.get("_summarization_session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise AssemblyVerificationError(
            f"_summarization_session_id 非法: {session_id!r}"
        )
    text = summary_message.text if summary_message.content else ""
    return text or None


# ============================================================================
# 模型构造（§10.1 步骤 1；build_db_agent 在步骤 4 交付）
# ============================================================================


def build_model(cfg: DbAgentConfig) -> BaseChatModel:
    """构造主模型并设置请求超时（§11.1）。

    OpenAI 兼容路径：timeout 作为工厂覆盖参数在构造时传入（映射为
    request_timeout，客户端按该值构建）；显式配置的 overrides.timeout 优先。
    ChatTongyi：工厂拒绝 timeout 覆盖（C12），构造后经 apply_request_timeout
    写入 model_kwargs（DashScope SDK 每次调用时读取）。
    """
    from src.services.config_loader import get_config
    from src.services.llm_factory import get_llm

    profile = get_config().get("llm_profiles", {}).get(cfg.model.llm_profile) or {}
    provider = profile.get("provider", "")
    overrides = dict(cfg.model.overrides)
    if provider != "dashscope":
        overrides.setdefault("timeout", cfg.model.request_timeout_seconds)
    model = get_llm(cfg.model.llm_profile, **overrides).llm
    if provider == "dashscope":
        apply_request_timeout(model, cfg.model.request_timeout_seconds)
    return model


# ============================================================================
# 步骤 4：装配（§10.1、§10.3、§10.6）
# ============================================================================

MODULE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 阶段工具清单常量（§10.6：代码定义，装配检查与运行期审计同源）
# ---------------------------------------------------------------------------

_FILE_TOOLS = ("ls", "read_file", "glob", "grep")
_RETRIEVAL_TOOLS = ("search_schema", "inspect_tables", "plan_joins")
_EVIDENCE_TOOLS = ("resolve_time_range", "probe_dimension_values", "reuse_evidence")
_SQL_TOOLS = ("validate_sql", "execute_sql")
_RESULT_TOOLS = ("read_result", "analyze_result")  # 步骤 5 交付
_SUBMIT_TOOL = "submit_final_answer"

# 步骤 4：9（业务+提交）+ 4（文件）= 13；步骤 5：11 + 4 = 15（§10.6）
STEP4_TOOLS = frozenset(
    _FILE_TOOLS + _RETRIEVAL_TOOLS + _EVIDENCE_TOOLS + _SQL_TOOLS + (_SUBMIT_TOOL,)
)
FULL_TOOLS = frozenset(set(STEP4_TOOLS) | set(_RESULT_TOOLS))

# 自定义工具的模型可见字段（§7 约定；check 5 用）
TOOL_VISIBLE_FIELDS: dict[str, tuple[list[str], list[str]]] = {
    # name: (properties, required)
    "search_schema": (["query", "kinds", "value_texts", "top_k"], ["query"]),
    "inspect_tables": (["table_ids", "include_live_columns"], ["table_ids"]),
    "plan_joins": (
        ["base_table", "target_tables", "max_hops"],
        ["base_table", "target_tables"],
    ),
    "resolve_time_range": (
        [
            "source_text",
            "kind",
            "relative",
            "n",
            "year",
            "quarter",
            "month",
            "day",
            "start_date",
            "end_date_inclusive",
            "boundary_date",
            "relation",
        ],
        ["source_text", "kind"],
    ),
    "probe_dimension_values": (
        ["table_id", "column", "mode", "value_text", "limit"],
        ["table_id", "column", "mode"],
    ),
    "reuse_evidence": (["evidence_ids"], ["evidence_ids"]),
    "validate_sql": (["sql"], ["sql"]),
    "execute_sql": (["sql"], ["sql"]),
    _SUBMIT_TOOL: (
        [
            "status",
            "answer",
            "findings",
            "metric_definitions",
            "limitations",
            "open_questions",
        ],
        ["status", "answer"],
    ),
    "read_result": (
        ["result_ref", "columns", "offset", "limit"],
        ["result_ref"],
    ),
    "analyze_result": (
        ["operation", "inputs", "params", "allow_pushdown"],
        ["operation", "inputs", "params"],
    ),
}


def stage_expected_tool_names(
    cfg: "DbAgentConfig", *, include_result_tools: bool = True
) -> set[str]:
    """阶段期望工具集合（§10.6）：按 features 计算；装配与运行期审计共用。

    步骤 5 起默认 15 件（含 read_result／analyze_result）；阶段 4 口径
    以 include_result_tools=False 显式请求（测试兼容）。
    """
    names = set(FULL_TOOLS if include_result_tools else STEP4_TOOLS)
    if cfg.features.todo_list:
        names.add("write_todos")
    return names


# ---------------------------------------------------------------------------
# Backend 与权限（§10.3）
# ---------------------------------------------------------------------------

_HIDDEN_PATHS = ["/**/.*", "/**/.*/**"]


def build_session_dir(cfg: "DbAgentConfig", session_id: str) -> Path:
    # ID 校验先于目录副作用（doc 2 §4.2）：非法 session_id 不得创建目录
    from src.modules.db_deepagent.persistence.checkpoint import valid_web_id

    if not valid_web_id(session_id):
        raise ValueError(
            f"session_id 非法（允许 [A-Za-z0-9_-]，长度 1–64，禁冒号）: "
            f"{session_id!r}"
        )
    session_dir = Path(cfg.workspace.root) / "sessions" / session_id
    (session_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (session_dir / "empty_root").mkdir(parents=True, exist_ok=True)
    return session_dir


def build_permissions(cfg: "DbAgentConfig") -> list[Any]:
    from deepagents import FilesystemPermission

    permissions = [
        FilesystemPermission(
            operations=["read", "write"], paths=_HIDDEN_PATHS, mode="deny"
        ),
        FilesystemPermission(
            operations=["read"], paths=["/skills", "/skills/**"], mode="allow"
        ),
        FilesystemPermission(
            operations=["read"], paths=["/artifacts", "/artifacts/**"], mode="allow"
        ),
    ]
    if cfg.features.memory:
        permissions.append(
            FilesystemPermission(
                operations=["read"], paths=["/memory/memory_context.md"], mode="allow"
            )
        )
    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/", "/**", *_HIDDEN_PATHS],
            mode="deny",
        )
    )
    return permissions


def build_backend(session_dir: Path, cfg: "DbAgentConfig"):
    from deepagents.backends import CompositeBackend, FilesystemBackend

    routes: dict[str, Any] = {
        "/skills/": FilesystemBackend(
            root_dir=MODULE_DIR / "skills", virtual_mode=True
        ),
        "/artifacts/": FilesystemBackend(
            root_dir=session_dir / "artifacts", virtual_mode=True
        ),
    }
    if cfg.features.memory:
        routes["/memory/"] = FilesystemBackend(
            root_dir=MODULE_DIR / "memory", virtual_mode=True
        )
    return CompositeBackend(
        default=FilesystemBackend(
            root_dir=session_dir / "empty_root", virtual_mode=True
        ),
        routes=routes,
        artifacts_root="/artifacts",
    )


def build_filesystem_middleware(backend, permissions, cfg: "DbAgentConfig"):
    """§10.2 适配点：白名单实例，显式传 permissions 与转存阈值。

    DeepAgents 0.7.31 起公开 `permissions=`（`_permissions` 弃用，0.9.0 移除）；
    0.7.34 对自定义文件中间件做 Agent 权限继承与冲突检查，本函数必须与
    `create_deep_agent(permissions=...)` 传同一组规则（doc 1 §4.1）。
    """
    from deepagents import FilesystemMiddleware

    return FilesystemMiddleware(
        backend=backend,
        tools=list(_FILE_TOOLS),
        tool_token_limit_before_evict=cfg.filesystem.tool_token_limit_before_evict,
        permissions=permissions,
    )


def load_prompts() -> dict[str, Any]:
    import yaml

    path = MODULE_DIR / "config" / "prompts.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_summary_prompt(prompts: dict[str, Any]) -> str:
    from deepagents.middleware.summarization import DEEPAGENTS_DEFAULT_SUMMARY_PROMPT

    return (
        prompts.get("agent", {}).get("summary_prompt")
        or DEEPAGENTS_DEFAULT_SUMMARY_PROMPT
    )


# ---------------------------------------------------------------------------
# 装配后验证（§10.6）
# ---------------------------------------------------------------------------


def verify_assembly(
    agent, cfg: "DbAgentConfig", *, include_result_tools: bool = True
) -> set[str]:
    """创建后立即执行；失败抛 AssemblyVerificationError，不进入会话（步骤 5：15 件）。"""
    from langgraph.channels import DeltaChannel

    expected = stage_expected_tool_names(cfg, include_result_tools=include_result_tools)
    tools_node = agent.nodes.get("tools")
    bound = getattr(getattr(tools_node, "bound", None), "tools_by_name", None) or {}
    actual = set(bound)
    if actual != expected:
        raise AssemblyVerificationError(
            f"工具集合不符：missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    messages_channel = agent.channels.get("messages")
    if not isinstance(messages_channel, DeltaChannel):
        raise AssemblyVerificationError("messages 通道不是 DeltaChannel")
    if "files" in agent.channels:
        raise AssemblyVerificationError("出现 files 通道（不应使用 StateBackend）")
    submit = bound.get(_SUBMIT_TOOL)
    if submit is None or getattr(submit, "return_direct", None) is not False:
        raise AssemblyVerificationError(
            "submit_final_answer 缺失或 return_direct 非 False"
        )
    for name, (fields, required) in TOOL_VISIBLE_FIELDS.items():
        tool = bound.get(name)
        if tool is None:
            continue  # 步骤 5 前结果工具不在集合中
        schema = tool.tool_call_schema.model_json_schema()
        props = set(schema.get("properties") or {})
        if "runtime" in props:
            raise AssemblyVerificationError(f"{name} 可见 Schema 含 runtime")
        if props != set(fields):
            raise AssemblyVerificationError(
                f"{name} 可见字段不符：missing={sorted(set(fields) - props)} extra={sorted(props - set(fields))}"
            )
        if not set(required) <= set(schema.get("required") or []):
            raise AssemblyVerificationError(f"{name} required 缺少约定必填项")
    return expected


# ---------------------------------------------------------------------------
# build_db_agent（§10.1）
# ---------------------------------------------------------------------------


class DbAgentHandle:
    """装配产物（§10.1）：agent 与会话级资源"""

    def __init__(
        self, agent, backend, permissions, harness_key: str, session_dir: Path
    ):
        self.agent = agent
        self.backend = backend
        self.permissions = permissions
        self.harness_key = harness_key
        self.session_dir = session_dir


def build_db_agent(
    services,
    *,
    session_id: str,
    checkpointer=None,
    store=None,
    include_result_tools: bool = True,
) -> DbAgentHandle:
    """按 §10.1 顺序装配；verify_assembly 失败即抛错（步骤 5：15 件口径）。"""
    from deepagents import create_deep_agent
    from deepagents.middleware.summarization import SummarizationMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

    from src.modules.db_deepagent.middleware import (
        FinalAnswerGateMiddleware,
        RequestAuditMiddleware,
        RunBudgetMiddleware,
    )
    from src.modules.db_deepagent.tools.final_answer import (
        make_submit_final_answer_tool,
    )

    cfg = services.config
    model = build_model(cfg)
    harness_key = register_and_verify_harness_profile(model, cfg)
    session_dir = build_session_dir(cfg, session_id)
    backend = build_backend(session_dir, cfg)
    permissions = build_permissions(cfg)
    file_mw = build_filesystem_middleware(backend, permissions, cfg)

    prompts = load_prompts()
    summary_model = model.model_copy()
    summary_model.metadata = {
        **(model.metadata or {}),
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

    extra_mw = [
        FinalAnswerGateMiddleware(services.budget, cfg.budget),
        RunBudgetMiddleware(services.budget, cfg.budget),
        RequestAuditMiddleware(
            expected_tool_names=lambda: stage_expected_tool_names(
                cfg, include_result_tools=include_result_tools
            ),
            budget=services.budget,
            workspace_root=Path(cfg.workspace.root),
        ),
    ]
    if cfg.features.todo_list:
        from langchain.agents.middleware import TodoListMiddleware

        extra_mw.append(TodoListMiddleware())

    tools: list[BaseTool] = list(build_business_tools(services))
    if not include_result_tools:
        from src.modules.db_deepagent.tools.schema_tools import (
            make_inspect_tables_tool,
            make_plan_joins_tool,
            make_search_schema_tool,
        )
        from src.modules.db_deepagent.tools.guard_tools import (
            make_probe_dimension_values_tool,
            make_resolve_time_range_tool,
            make_reuse_evidence_tool,
        )
        from src.modules.db_deepagent.tools.sql_tools import (
            make_execute_sql_tool,
            make_validate_sql_tool,
        )

        tools = [
            make_search_schema_tool(services),
            make_inspect_tables_tool(services),
            make_plan_joins_tool(services),
            make_resolve_time_range_tool(services),
            make_probe_dimension_values_tool(services),
            make_reuse_evidence_tool(services),
            make_validate_sql_tool(services),
            make_execute_sql_tool(services),
        ]
    tools.append(make_submit_final_answer_tool())

    agent = create_deep_agent(
        model=model,
        system_prompt=prompts["agent"]["system_prompt"],
        tools=tools,
        middleware=[file_mw, summary_mw, PatchToolCallsMiddleware(), *extra_mw],
        subagents=[],
        skills=["/skills/"],
        memory=(["/memory/memory_context.md"] if cfg.features.memory else None),
        permissions=permissions,
        backend=backend,
        state_schema=DbDeepAgentState,
        context_schema=DbAgentContext,
        checkpointer=checkpointer,
        store=store,
        name="db_deepagent",
    )
    verify_assembly(agent, cfg, include_result_tools=include_result_tools)
    logger.info(
        "db_deepagent 已装配（session=%s harness=%s tools=%d）",
        session_id,
        harness_key,
        len(stage_expected_tool_names(cfg, include_result_tools=include_result_tools)),
    )
    return DbAgentHandle(agent, backend, permissions, harness_key, session_dir)
