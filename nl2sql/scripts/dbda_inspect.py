"""db_deepagent 会话检查工具（只读）

从平台库 checkpoint 读出指定线程的完整消息历史，用于调试：
每步的模型思考文本、工具调用参数、工具返回原文。

用法：
    uv run python scripts/dbda_inspect.py --session my-session          # 最新线程
    uv run python scripts/dbda_inspect.py --session my-session --seq 1  # 指定线程
    uv run python scripts/dbda_inspect.py --session my-session --full   # 不截断输出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="db_deepagent 会话检查（只读）")
    parser.add_argument("--session", required=True, help="会话 ID")
    parser.add_argument("--seq", type=int, default=None, help="线程序号（默认最新）")
    parser.add_argument("--full", action="store_true", help="完整输出（不截断）")
    args = parser.parse_args()

    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import END, START, StateGraph

    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.persistence.archive import QaArchive
    from src.modules.db_deepagent.persistence.platform_pool import build_platform_pool
    from src.modules.db_deepagent.state import DbDeepAgentState

    cfg = load_db_agent_config()
    pool = build_platform_pool(cfg.platform_db)
    pool.open(wait=False)
    pool.wait()
    try:
        # 线程序号：显式指定 > sessions 记录的当前线程
        if args.seq is not None:
            thread_id = f"dbda:{args.session}:{args.seq}"
        else:
            session = QaArchive(
                __import__(
                    "langgraph.store.postgres", fromlist=["PostgresStore"]
                ).PostgresStore(pool)
            ).get_session(args.session)
            current = (session or {}).get("current_thread_id")
            if not current:
                print(f"会话 {args.session} 无 sessions 记录，用 --seq 指定线程")
                return 2
            thread_id = current

        saver = PostgresSaver(pool)
        builder = StateGraph(DbDeepAgentState)
        builder.add_node("noop", lambda s: {"messages": []})
        builder.add_edge(START, "noop")
        builder.add_edge("noop", END)
        graph = builder.compile(checkpointer=saver)

        snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
        messages = (snapshot.values or {}).get("messages") or []
        limit = None if args.full else 90
        print(f"线程 {thread_id}：共 {len(messages)} 条消息")
        print("=" * 70)
        seen_question = False
        for i, message in enumerate(messages):
            kind = type(message).__name__
            text = (
                (message.content or "").strip()
                if isinstance(message.content, str)
                else str(message.content or "")
            )
            if kind == "HumanMessage":
                is_handoff = (message.additional_kwargs or {}).get(
                    "dbda_kind"
                ) == "handoff"
                if seen_question and not is_handoff:  # 新一轮问题前打印分隔符
                    print("\n" + "-" * 70)
                seen_question = True
                label = "会话交接" if is_handoff else "用户"
                print(f"[{i}] {label}: {text[:limit]}")
            elif kind == "AIMessage":
                if text:
                    print(f"[{i}] 模型文本: {text[:limit]}")
                for call in message.tool_calls or []:
                    arguments = json.dumps(call["args"], ensure_ascii=False)
                    print(f"[{i}] 模型调工具: {call['name']}({arguments[:limit]})")
            elif kind == "ToolMessage":
                body = text[: limit or None].replace("\n", " ") if limit else text
                print(f"[{i}]   -> 工具返回: {body}")
            else:
                print(f"[{i}] {kind}: {text[:limit]}")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
