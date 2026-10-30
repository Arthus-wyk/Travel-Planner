"""从唯一注册表选择工具；能力上限由服务端提供的 scope 决定。"""

from tooling.registry import ToolRegistry


def select_tools(
    registry: ToolRegistry, domains: list[str], scope: frozenset[str] | None
) -> set[str]:
    available = registry.names if scope is None else registry.names & scope
    selected = {spec.name for spec in registry.for_domains(domains)} & available
    if selected:
        return selected
    specs = [spec for spec in registry.all() if spec.name in available]
    fallback = {spec.name for spec in specs if spec.metadata.get("default_visible")}
    # 旅行工具未知意图默认只读；专业 Agent 的范围由调用者显式配置。
    return (
        fallback
        if any("default_visible" in spec.metadata for spec in specs)
        else available
    )
