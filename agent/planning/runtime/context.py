from contextvars import ContextVar

generation_job_id = ContextVar("generation_job_id", default="-")
