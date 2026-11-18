"""One shared model/tool loop; role behavior is supplied through context."""
from langgraph.graph import END, START, StateGraph
from agent.chat import nodes
from agent.chat.context import AgentContext
from agent.chat.state import AgentState


def after_model(state: AgentState) -> str:
    if state["done"]:
        return "finish"
    return "tools" if state["calls"] else "model"


def build_graph():
    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("model", nodes.call_model)
    graph.add_node("tools", nodes.execute_tool)
    graph.add_node("finish", nodes.finish)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", after_model, ["finish", "tools", "model"])
    graph.add_edge("tools", "model")
    graph.add_edge("finish", END)
    return graph.compile()
