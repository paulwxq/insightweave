"""运行级上下文 DbAgentContext（docs/v4/5 §5.1）

每次 agent.invoke 由 Runner 新建，通过 context= 传入，工具经 runtime.context 读取。
模型无法修改。user_texts 的构造规则（排除 dbda_kind 标记的系统注入 HumanMessage）
在 Runner 实现（§10），这里只承载结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.modules.db_deepagent.schemas import CarriedEvidence


@dataclass(frozen=True)
class DbAgentContext:
    """单次 invoke 的运行上下文（frozen：工具与模型均不可篡改）"""

    session_id: str  # CLI 会话；线程轮换时保持不变；结果归属以它为准
    thread_id: str  # 当前 checkpoint 线程
    request_id: str  # 本轮用户请求
    run_id: str  # 服务端每次 invoke 新建（uuid4），恢复执行也新建
    datasource_id: str  # 业务数据源指纹（§6.1）
    validation_fingerprint: str  # 校验配置指纹（§4.2）
    now: datetime  # 带时区；fixed_now 或实时时钟
    timezone: str  # Asia/Shanghai
    user_texts: tuple[str, ...]  # 本线程全部用户原文，最后一项为本轮问题
    carried_evidence: tuple[CarriedEvidence, ...]  # 线程轮换时携带的已确认证据（§13.4）
    deadline_monotonic: float  # 运行时限（time.monotonic 基准）
