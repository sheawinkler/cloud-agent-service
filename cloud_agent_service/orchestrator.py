from __future__ import annotations

from queue import Empty, Queue

from cloud_agent_service.models import JobResult
from cloud_agent_service.pipeline import AgentCloudFlow


class LocalJobQueue:
    def __init__(self) -> None:
        self._queue: Queue[str] = Queue()

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def dequeue(self) -> str | None:
        try:
            return self._queue.get_nowait()
        except Empty:
            return None


class LocalOrchestrator:
    def __init__(self, flow: AgentCloudFlow, job_queue: LocalJobQueue) -> None:
        self.flow = flow
        self.job_queue = job_queue

    def submit(self, job_id: str) -> None:
        self.job_queue.enqueue(job_id)

    def run_queued_once(self) -> JobResult | None:
        job_id = self.job_queue.dequeue()
        if not job_id:
            return None
        try:
            return self.flow.run_job(job_id)
        except Exception:
            return None
