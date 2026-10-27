"""Background jobs for CPU-heavy work (replications, comparisons, optimization).

Replications run in a process pool so they use all cores and never block the
event loop that drives live WebSocket sessions. Jobs live in memory: they are
cheap to recompute, and a restart simply loses in-flight results.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import secrets
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from experiments.runner import ReplicationResult, run_replication
from sim import Scenario

log = logging.getLogger("factory.jobs")

JOB_TTL_SECONDS = 3600.0


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TooManyJobsError(RuntimeError):
    pass


ProgressFn = Callable[[float], None]


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    created: float = field(default_factory=time.time)
    finished: float | None = None
    result: Any = None
    error: str | None = None
    task: asyncio.Task[None] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "progress": round(self.progress, 4),
            "error": self.error,
            "result": self.result,
        }


class JobManager:
    def __init__(self, workers: int, max_active: int) -> None:
        self._workers = workers
        self._max_active = max_active
        self._jobs: dict[str, Job] = {}
        self._executor: ProcessPoolExecutor | None = None

    @property
    def executor(self) -> ProcessPoolExecutor:
        if self._executor is None:
            # "spawn" avoids forking a process that already runs threads (uvicorn).
            self._executor = ProcessPoolExecutor(self._workers, mp_context=multiprocessing.get_context("spawn"))
        return self._executor

    async def map_replications(
        self, scenario: Scenario, seeds: list[int], horizon: float, warmup: float, progress: ProgressFn | None = None
    ) -> list[ReplicationResult]:
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(self.executor, run_replication, scenario, s, horizon, warmup) for s in seeds]
        results: list[ReplicationResult] = []
        for fut in asyncio.as_completed(futures):
            results.append(await fut)
            if progress:
                progress(len(results) / len(seeds))
        return results

    def submit(self, kind: str, work: Callable[[ProgressFn], Awaitable[Any]]) -> Job:
        self._expire()
        active = sum(1 for j in self._jobs.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING))
        if active >= self._max_active:
            raise TooManyJobsError
        job = Job(id=secrets.token_urlsafe(12), kind=kind)
        self._jobs[job.id] = job

        def set_progress(p: float) -> None:
            job.progress = max(job.progress, min(1.0, p))

        async def runner() -> None:
            job.status = JobStatus.RUNNING
            started = time.perf_counter()
            try:
                job.result = await work(set_progress)
                job.status = JobStatus.DONE
                job.progress = 1.0
                log.info("job done id=%s kind=%s seconds=%.2f", job.id, kind, time.perf_counter() - started)
            except asyncio.CancelledError:
                job.status, job.error = JobStatus.FAILED, "Cancelled"
                raise
            except ValueError as exc:  # invalid input discovered while running
                job.status, job.error = JobStatus.FAILED, str(exc)
                log.warning("job rejected id=%s kind=%s error=%s", job.id, kind, exc)
            except Exception:
                job.status, job.error = JobStatus.FAILED, "The job failed unexpectedly. Please try again."
                log.exception("job failed id=%s kind=%s", job.id, kind)
            finally:
                job.finished = time.time()

        job.task = asyncio.create_task(runner())
        return job

    def get(self, job_id: str) -> Job | None:
        self._expire()
        return self._jobs.get(job_id)

    def _expire(self) -> None:
        cutoff = time.time() - JOB_TTL_SECONDS
        for jid in [j.id for j in self._jobs.values() if j.finished and j.finished < cutoff]:
            del self._jobs[jid]

    async def shutdown(self) -> None:
        for job in self._jobs.values():
            if job.task and not job.task.done():
                job.task.cancel()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
