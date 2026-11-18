"""父 Agent 的单层专业任务委派工具契约。"""

from collections.abc import Iterable

from tooling.registry import ToolRegistry
from tooling.spec import ToolHandler, ToolSpec


def delegation_registry(handler: ToolHandler, roles: Iterable[str]) -> ToolRegistry:
    """仅暴露服务器允许的专业角色，不接受模型指定的 Prompt 或权限。"""
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["task_id", "role", "task", "extra_data"],
        "properties": {
            "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "role": {"type": "string", "enum": sorted(roles)},
            "task": {"type": "string", "minLength": 1, "maxLength": 4000},
            "extra_data": {
                "type": "object", "additionalProperties": False,
                "required": ["facts", "artifact_refs"],
                "properties": {
                    "facts": {"type": "string", "maxLength": 12000},
                    "artifact_refs": {"type": "array", "maxItems": 32,
                                      "items": {"type": "string", "minLength": 1, "maxLength": 128}},
                },
            },
        },
    }
    return ToolRegistry([ToolSpec(
        "delegate_task", "委派专业审核任务；相同 task_id 继续原会话及剩余预算。"
        "extra_data 仅含事实及当前运行的成果引用，不能授予权限。",
        schema, handler,
    )])
