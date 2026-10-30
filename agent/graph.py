"""只定义工作流。节点实现、资源生命周期和结果持久化都不放在这里。"""

from langgraph.graph import END, START, StateGraph
from agent import nodes
from agent.context import AgentContext
from agent.state import AgentState


def after_model(state: AgentState) -> str:
    if state["done"]:
        return "finish"
    return "tools" if state["calls"] else "model"


def after_verification(state: AgentState) -> str:
    return "tools" if state["call_index"] < len(state["calls"]) else "model"


def build_graph():
    # context_schema 传递请求私有资源，图状态只携带普通数据，便于后续作为子图使用。
    graph = StateGraph(AgentState, context_schema=AgentContext)
    for name, node in {
        "prepare": nodes.prepare,
        "understand": nodes.understand_request,
        "route": nodes.route,
        "model": nodes.call_model,
        "tools": nodes.execute_tool,
        "verify": nodes.verify_tool,
        "finish": nodes.finish,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "understand")
    graph.add_edge("understand", "route")
    graph.add_edge("route", "model")
    graph.add_conditional_edges("model", after_model, ["finish", "tools", "model"])
    graph.add_edge("tools", "verify")
    graph.add_conditional_edges("verify", after_verification, ["tools", "model"])
    graph.add_edge("finish", END)
    return graph.compile()
