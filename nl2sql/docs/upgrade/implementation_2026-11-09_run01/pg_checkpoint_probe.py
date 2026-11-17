"""PG checkpoint 跨版本验收探针（doc 1 §6.5）。

用法: pg_checkpoint_probe.py <mode>
  read-old-existing : 新环境冷读一条升级前真实线程（完整反序列化）
  write-and-cold-read: 新环境写验证线程 + 重新装配冷读
  read-by-old-env   : 旧基线环境回退读取新环境写入的验证线程
  cleanup           : 删除验证线程的 checkpoint 行（仅限本探针线程）

连接经既有 .env / 平台池机制；只读写 db_deepagent schema 的
checkpoints/checkpoint_blobs/checkpoint_writes，不触碰其他数据。
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/opt/projects/insightweave/nl2sql")

from deepagents import DeepAgentState
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

MODE = sys.argv[1]
VERIFY_THREAD = "dbda:upgrade_verify_20261109:1"
SAMPLE_OLD_THREAD = "dbda:s_9dc8ec10a5:1"  # 升级前旧 SDK 写入的短线程


def build_pool():
    from src.modules.db_deepagent.config import load_db_agent_config
    from src.modules.db_deepagent.persistence.platform_pool import build_platform_pool

    cfg = load_db_agent_config()
    return build_platform_pool(cfg.platform_db)


def compile_graph(saver):
    builder = StateGraph(DeepAgentState)
    builder.add_node("reply", lambda state: {"messages": [AIMessage(content="verify reply")]})
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=saver)


def main() -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    pool = build_pool()
    pool.open(wait=True)  # build_platform_pool 以 open=False 构建，调用方负责打开
    try:
        with pool.connection() as conn:
            saver = PostgresSaver(conn)
            config = {"configurable": {"thread_id": VERIFY_THREAD}}
            if MODE == "read-old-existing":
                cfg = {"configurable": {"thread_id": SAMPLE_OLD_THREAD}}
                graph = compile_graph(saver)
                values = graph.get_state(cfg).values
                msgs = values.get("messages") or []
                print(f"read-old-existing passed: messages={len(msgs)}")
            elif MODE == "write-and-cold-read":
                graph = compile_graph(saver)
                graph.invoke(
                    {"messages": [HumanMessage(content="upgrade verify round 1")]},
                    config,
                )
                values = graph.get_state(config).values
                assert len(values["messages"]) == 2
                # 冷读：同库新连接、重新装配图
                with pool.connection() as conn2:
                    cold = compile_graph(PostgresSaver(conn2))
                    cold_values = cold.get_state(config).values
                    assert len(cold_values["messages"]) == 2
                print("write-and-cold-read passed: messages=2 (warm+cold)")
            elif MODE == "read-by-old-env":
                graph = compile_graph(saver)
                values = graph.get_state(config).values
                assert len(values["messages"]) == 2
                print("read-by-old-env passed: messages=2")
            elif MODE == "cleanup":
                cur = conn.cursor()
                for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                    cur.execute(
                        f"delete from db_deepagent.{table} where thread_id = %s",
                        (VERIFY_THREAD,),
                    )
                    print(f"cleanup {table}: {cur.rowcount} rows")
                conn.commit()
            else:
                raise ValueError(MODE)
    finally:
        pool.close()


if __name__ == "__main__":
    main()
