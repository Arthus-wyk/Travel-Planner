"""将审核结果转为公开话术，内部输出继续保留在运行日志。"""

import json
import re

from agent.chat.review_execution import ReviewExecution, ReviewStepTimeout
from infrastructure.llm import RetryConfig, request_model_with_retry

INTERNAL_TEXT = re.compile(
    r"\b(?:\w+_id|\w+_agent|DAG|checkpoint|build_edits|search_places|audit_route)\b"
    r"|(?:子|父)\s*(?:agent|智能体)|编排|[a-f0-9]{32}|\bB0[A-Z0-9]{7,}\b|\bguided-[\w-]+",
    re.IGNORECASE,
)


def public_text(text: str, fallback: str, identifiers: set[str]) -> str:
    """在展示边界拦截内部词汇和已知标识。"""
    if not isinstance(text, str) or not text.strip() or INTERNAL_TEXT.search(text):
        return fallback
    if any(identifier and len(identifier) >= 5 and identifier in text for identifier in identifiers):
        return fallback
    return text


async def format_review_reply(client, model: str, execution: ReviewExecution,
                              facts: dict, fallback: str, identifiers: set[str]) -> str:
    """用独立提示词整理公开回复，异常时返回确定性模板。"""
    try:
        response, _, _ = await execution.run(request_model_with_retry(
            ollama=client, model=model, tools=[], config=RetryConfig(max_attempts=1), stage="review_public_reply",
            messages=[
                {"role": "system", "content": (
                    "你负责整理给旅行用户看的最终答复。只使用已提供的公开事实，简洁说清时间、地点和结果。"
                    "没有保存时不要声称已保存。不要展示标识、角色、工具、版本、任务编排或原始异常。"
                    "不要改变用户要选择的内容，不要调用工具，直接回复中文。"
                )},
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
        ), "整理回复")
    except (ReviewStepTimeout, ValueError):
        return fallback
    if response is None or response.message.tool_calls:
        return fallback
    content = public_text(response.message.content, fallback, identifiers)
    if facts["状态"] != "已保存" and re.search(r"已保存|已经保存|写入成功", content):
        return fallback
    return content
