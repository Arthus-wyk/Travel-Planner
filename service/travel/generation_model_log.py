"""生成任务的模型原始输出日志。"""

from service.utils.request_log import write_event
from service.utils.jsonl_log import to_jsonable


def log_model_output(stage: str, message, *, position: int | None = None) -> None:
    from agent.planning_graph import generation_job_id

    write_event("model.responses", {"job_id": generation_job_id.get(), "stage": stage,
                                    "position": position, "body": to_jsonable(message)})
