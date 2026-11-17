from __future__ import annotations

import pickle
import sys
from pathlib import Path

from deepagents import DeepAgentState
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


def plain(value):
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


mode, filename = sys.argv[1:]
path = Path(filename)
saver = InMemorySaver()
if path.exists():
    # Only consume the probe's own locally generated checkpoint data.
    with path.open("rb") as stream:
        saved = pickle.load(stream)
    for name, values in saved.items():
        getattr(saver, name).update(values)

builder = StateGraph(DeepAgentState)
builder.add_node("reply", lambda state: {"messages": [AIMessage(content="offline reply")]})
builder.add_edge(START, "reply")
builder.add_edge("reply", END)
graph = builder.compile(checkpointer=saver)
config = {"configurable": {"thread_id": "upgrade-probe"}}

if mode == "write-old":
    result = graph.invoke({"messages": [HumanMessage(content="round one")]}, config)
    assert len(result["messages"]) == 2
elif mode == "read-and-write-new":
    assert len(graph.get_state(config).values["messages"]) == 2
    result = graph.invoke({"messages": [HumanMessage(content="round two")]}, config)
    assert len(result["messages"]) == 4
    cold_graph = builder.compile(checkpointer=saver)
    assert len(cold_graph.get_state(config).values["messages"]) == 4
elif mode == "read-old":
    assert len(graph.get_state(config).values["messages"]) == 4
else:
    raise ValueError(mode)

with path.open("wb") as stream:
    pickle.dump({name: plain(getattr(saver, name)) for name in ("storage", "writes", "blobs")}, stream)
print(mode, "passed")
