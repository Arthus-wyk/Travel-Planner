"""审核 Agent 的 OpenAI 严格 JSON Schema 及业务结果读取。"""

from copy import deepcopy


def object_schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
PARENT_SCHEMA = object_schema({
    "kind": {"type": "string", "enum": ["answer", "clarify", "changes", "optimize"]},
    "message": TEXT,
    "question": {"type": ["string", "null"]}, "options": TEXTS,
    "candidate_groups": {"type": "array", "items": object_schema({
        "id": TEXT, "label": TEXT, "place_ids": TEXTS,
        "selection_count": {"type": ["integer", "null"], "minimum": 1},
    })},
})


def strict_schema(schema: dict) -> dict:
    """将业务 Schema 的可选字段转为 required + nullable，保留输入边界校验。"""
    result = deepcopy(schema)
    if "oneOf" in result:
        result["anyOf"] = result.pop("oneOf")
    if "const" in result:
        value = result.pop("const")
        result["enum"] = [value]
        if isinstance(value, str):
            result.setdefault("type", "string")
    for key in ("anyOf", "allOf"):
        if key in result:
            result[key] = [strict_schema(branch) for branch in result[key]]
    if result.get("type") == "object":
        required = result.get("required", [])
        properties = {}
        for name, value in result.get("properties", {}).items():
            value = strict_schema(value)
            if name not in required:
                value = {"anyOf": [value, {"type": "null"}]}
            properties[name] = value
        result.update(properties=properties, required=list(properties), additionalProperties=False)
    if "items" in result:
        result["items"] = strict_schema(result["items"])
    return result


def child_schema(role: str, operations_schema: dict) -> dict:
    properties = {"message": TEXT, "remaining": TEXTS}
    if role == "search_places":
        properties["place_ids"] = TEXTS
    if role == "build_edits":
        operations = strict_schema(operations_schema)
        operations["minItems"] = 0
        properties.update(kind={"type": "string", "enum": ["changes", "optimize", "clarify"]},
                          operations=operations)
    return object_schema({
        "status": {"type": "string", "enum": ["completed", "failed", "needs_replan"]},
        "data": object_schema(properties), "reason": TEXT,
    })


def read_operations(operations: list, schema: dict) -> list:
    """移除严格输出为业务可选字段补的 null，保留原本支持 null 的业务字段。"""
    variants = schema.get("items", {}).get("oneOf", [])
    result = []
    for operation in operations:
        variant = next((item for item in variants
                        if item["properties"]["action"].get("const") == operation.get("action")), None)
        if variant is None:
            result.append(operation)
            continue
        payload_schema = variant["properties"]["payload"]
        required = payload_schema.get("required", [])
        payload = {}
        for key, value in operation["payload"].items():
            field_type = payload_schema.get("properties", {}).get(key, {}).get("type", [])
            if value is not None or key in required or (isinstance(field_type, list) and "null" in field_type):
                payload[key] = value
        result.append({**operation, "payload": payload})
    return result
