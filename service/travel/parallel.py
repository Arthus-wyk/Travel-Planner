"""Run independent planning tasks with bounded concurrency and ordered results."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context


def ordered_parallel(jobs):
    jobs = list(jobs)
    if len(jobs) <= 1:
        return [copy_context().run(job) for job in jobs]
    # 每个任务继承生成日志上下文，汇总顺序与输入一致。
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="travel-planning") as pool:
        futures = [pool.submit(copy_context().run, job) for job in jobs]
        return [future.result() for future in futures]
